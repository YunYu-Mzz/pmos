'use strict';
// Bounded Playwright driver memory for long PMOS runs (port of Shanxi
// production's driver_memory_lifecycle / driver_route_lifecycle, 2026-10-31).
//
// Playwright 1.63 keeps every Request/Route/Response/Frame dispatcher until a
// 10,000-object bucket fills, and each small POST body can pin a 64 KiB Buffer
// slab. The request gate's route('**/*') per operation makes ~100 of them per
// call: the legacy coordinator reached ~1900 Requests and ~480 MiB swap in a
// few hours; with this module it stayed flat near 600 with no swap.
//
// What it does, invoked only by existing protocol activity (no timer, no
// browser operation, never a retry):
// - copies each business POST body into a private byte-identical buffer;
// - retires (native dispose 'gc', which also frees the Python side) settled
//   business requests older than 2 min beyond the latest 128, and their
//   finished routes, only while the worker's state allows it;
// - retires settled non-business page traffic (static, blocked by our gate,
//   anything settled 5+ min) beyond the latest 128;
// - retires detached frames seen detached for 5 min.
// Business URLs come from <state>/driver_business_urls.json (written by the
// worker from the specs); without that file nothing is retired.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const SDK_SHA = '549070af3acabb3efcc4f55bfe6210f9f7c2fcf633cf7eaa59bfe60719969171';
const KEY = Symbol.for('pmos.driver.memory.v1');
const KEEP = 128, MIN_AGE_MS = 120000, SWEEP_MS = 60000, SETTLED_AGE_MS = 300000;
const STATIC_TYPES = new Set(['document', 'stylesheet', 'image', 'media', 'font', 'script', 'texttrack', 'manifest']);

function businessUrls(state) {
  const value = JSON.parse(fs.readFileSync(path.join(state, 'driver_business_urls.json'), 'utf8'));
  const urls = new Set((value.urls || []).filter(u => typeof u === 'string' && /^https:\/\/[^/]+\/[A-Za-z0-9_./-]+$/.test(u)));
  if (!urls.size) throw Error('no_business_urls');
  return urls;
}

// Business objects are retired only while the worker that owns this driver is
// alive, writing status, and not blocked (a block keeps everything for review).
function stateAllowsRetirement(state) {
  try {
    if (fs.existsSync(path.join(state, 'block.json'))) return false;
    const status = JSON.parse(fs.readFileSync(path.join(state, 'status.json'), 'utf8'));
    const age = Date.now() - Date.parse(status.updated_at);
    return status.pid === process.ppid && Number.isFinite(age) && age >= -5000 && age < 120000;
  } catch { return false; }
}

function compactRequest(dispatcher, urls) {
  if (dispatcher._type !== 'Request' || dispatcher._disposed || !urls.has(dispatcher._object?._url)) return null;
  const request = dispatcher._object, old = request._postData;
  if (!Buffer.isBuffer(old) || old.byteLength === 0 || old.buffer.byteLength <= old.byteLength) return null;
  // allocUnsafeSlow has no shared slab. Never decode, print, hash or persist
  // the body; compare in RAM before replacing the sole request-owned reference.
  const copy = Buffer.allocUnsafeSlow(old.byteLength);
  old.copy(copy);
  if (!copy.equals(old) || copy.buffer.byteLength !== old.byteLength) throw Error('private_buffer_copy_failed');
  request._postData = copy;
  return {bodyBytes: copy.byteLength, priorBackingBytes: old.buffer.byteLength};
}

function activeGuids(connection) {
  const guids = new Set();
  for (const call of connection._activeProgressControllers.values()) {
    const guid = call.controller?.metadata?.objectId;
    if (typeof guid === 'string') guids.add(guid);
  }
  return guids;
}

function eligibleRequest(d, urls, now, protectedGuids) {
  if (d._type !== 'Request' || d._disposed || !urls.has(d._object?._url)) return false;
  const request = d._object, response = request._response;
  // Failed and pending requests stay; so does anything still in use.
  if (request._failureText || !response?._finishedPromise?.isDone() || response._status >= 400
      || !Number.isFinite(request._wallTimeMs) || now - request._wallTimeMs < MIN_AGE_MS) return false;
  if (protectedGuids.has(d._guid)) return false;
  for (const child of d._dispatchers.values()) {
    if (child._type !== 'Response' || protectedGuids.has(child._guid)) return false;
  }
  return true;
}

// Finished routes of settled business requests (the gate continued them).
function sweepRoutes(connection, urls, now, protectedGuids) {
  const requests = [...connection._dispatcherByGuid.values()].filter(d =>
    d._type === 'Request' && !d._disposed && urls.has(d._object?._url));
  let retired = 0;
  for (const d of requests.slice(0, Math.max(0, requests.length - KEEP))) {
    const r = d._object, res = r._response;
    if (r._failureText || !res?._finishedPromise?.isDone() || res._status !== 200 || !Number.isFinite(r._wallTimeMs)
        || now - r._wallTimeMs < MIN_AGE_MS || protectedGuids.has(d._guid)) continue;
    for (const child of [...d._dispatchers.values()]) {
      const route = child._object, ctx = r._context;
      if (child._type !== 'Route' || child._disposed || protectedGuids.has(child._guid) || child._dispatchers.size) continue;
      if (child._handled !== true || route?._handled !== true || route._request !== r || route._currentHandler !== undefined
          || !Array.isArray(route._futureHandlers) || route._futureHandlers.length) continue;
      // _handled is set before I/O: also require that native endHandling took
      // it out of the context's in-flight set. A changed SDK shape fails closed.
      if (!(ctx?._routesInFlight instanceof Set) || ctx._routesInFlight.has(route)) continue;
      child._dispose('gc'); retired++;
    }
  }
  return retired;
}

function sweep(connection, urls, now, stateAllowed) {
  if (!stateAllowed) return {retired: 0, retiredRoutes: 0, skipped: true};
  const protectedGuids = activeGuids(connection);
  const retiredRoutes = sweepRoutes(connection, urls, now, protectedGuids);
  const owned = [...connection._dispatcherByGuid.values()].filter(d =>
    d._type === 'Request' && !d._disposed && urls.has(d._object?._url));
  // Registration order is chronological: the latest 128 always stay.
  let retired = 0;
  for (const d of owned.slice(0, Math.max(0, owned.length - KEEP))) {
    if (!eligibleRequest(d, urls, now, protectedGuids)) continue;
    // Native GC disposes the response child and notifies the Python side.
    // Never close(), stopPendingOperations(), drop() or touch the browser.
    d._dispose('gc'); retired++;
  }
  return {retired, retiredRoutes, skipped: false};
}

// Page traffic that is never collection evidence: finished static GETs,
// requests our gate aborted, anything else settled for SETTLED_AGE_MS (far
// beyond any operation's timeout, so no Python callback can still need it).
function transientRequest(d, urls, now, protectedGuids) {
  if (d._type !== 'Request' || d._disposed || urls.has(d._object?._url)) return false;
  const request = d._object, age = now - request._wallTimeMs;
  if (!Number.isFinite(request._wallTimeMs) || age < MIN_AGE_MS) return false;
  const blocked = /ERR_BLOCKED_BY_CLIENT/.test(request._failureText || '');
  const finished = !request._failureText && request._response?._finishedPromise?.isDone();
  const staticDone = (request._method === 'GET' || request._method === 'HEAD') && STATIC_TYPES.has(request._resourceType) && finished;
  const settled = age >= SETTLED_AGE_MS && (finished || !!request._failureText);
  if ((!blocked && !staticDone && !settled) || protectedGuids.has(d._guid)) return false;
  for (const child of d._dispatchers.values()) {
    if (protectedGuids.has(child._guid)) return false;
    if (child._type === 'Route' ? child._handled !== true : child._type !== 'Response') return false;
  }
  return true;
}

function sweepTransient(connection, urls, now) {
  const protectedGuids = activeGuids(connection);
  const candidates = [...connection._dispatcherByGuid.values()].filter(d =>
    d._type === 'Request' && !d._disposed && !urls.has(d._object?._url));
  let retired = 0;
  for (const d of candidates.slice(0, Math.max(0, candidates.length - KEEP))) {
    if (!transientRequest(d, urls, now, protectedGuids)) continue;
    d._dispose('gc'); retired++;
  }
  return retired;
}

// A detached iframe's Frame object stays until the page closes: release one
// seen detached for SETTLED_AGE_MS with no children (the browser freed it).
const detachedSince = new WeakMap();
function sweepDetachedFrames(connection, now, protectedGuids) {
  let retired = 0;
  for (const d of [...connection._dispatcherByGuid.values()]) {
    if (d._type !== 'Frame' || d._disposed || d._gcBucket !== 'Frame') continue;
    const frame = d._object, manager = frame?._page?.frameManager;
    if (!manager?._frames || manager._frames.get(frame._id) === frame) { detachedSince.delete(d); continue; }
    if (!detachedSince.has(d)) detachedSince.set(d, now);
    if (now - detachedSince.get(d) < SETTLED_AGE_MS || d._dispatchers.size || protectedGuids.has(d._guid)) continue;
    d._dispose('gc'); retired++;
  }
  return retired;
}

function install({state, bundle, existingConnections = []}) {
  state = fs.realpathSync(state); bundle = fs.realpathSync(bundle);
  if (globalThis[KEY]) {
    if (globalThis[KEY].state !== state || globalThis[KEY].bundle !== bundle) throw Error('driver_patch_owner_changed');
    return globalThis[KEY];
  }
  const sha = crypto.createHash('sha256').update(fs.readFileSync(bundle)).digest('hex');
  if (sha !== SDK_SHA) throw Error('unreviewed_playwright_driver');
  const core = require(bundle);
  if (core.getPlaywrightVersion() !== '1.63.0') throw Error('unreviewed_playwright_version');
  if (!Array.isArray(existingConnections) || existingConnections.some(c => !(c instanceof core.server.DispatcherConnection)))
    throw Error('foreign_driver_connection');
  const counters = {copiedBodies: 0, copiedBytes: 0, retiredRequests: 0, retiredRoutes: 0, retiredTransient: 0,
                    retiredFrames: 0, maintenanceErrors: 0, skippedSweeps: 0, urlErrors: 0};
  let urls = null, urlsMtime = null;
  const currentUrls = () => {
    // Re-read when the worker rewrites the list; a missing or bad file retires nothing.
    try {
      const mtime = fs.statSync(path.join(state, 'driver_business_urls.json')).mtimeMs;
      if (mtime !== urlsMtime) { urls = businessUrls(state); urlsMtime = mtime; }
    } catch { urls = null; urlsMtime = null; counters.urlErrors++; }
    return urls;
  };
  const lastSweep = new WeakMap();
  const report = () => ({version: 1, sdkSha: SDK_SHA, pid: process.pid, parentPid: process.ppid, ...counters,
    businessUrls: urls ? urls.size : 0, memory: process.memoryUsage(), keepLatest: KEEP, minimumAgeMs: MIN_AGE_MS,
    stateAllowsRetirement: stateAllowsRetirement(state), browserOperations: 0, createdTimers: 0});
  const save = () => {
    const file = path.join(state, 'driver_memory.json'), temp = file + '.tmp.' + process.pid;
    fs.writeFileSync(temp, JSON.stringify({...report(), at: new Date().toISOString()}, null, 2) + '\n', {mode: 0o600});
    fs.renameSync(temp, file);
  };
  function compact(d) {
    const known = urls || currentUrls();
    const result = known && compactRequest(d, known);
    if (result) { counters.copiedBodies++; counters.copiedBytes += result.bodyBytes; }
  }
  function maintain(connection, force = false) {
    const now = Date.now();
    if (!force && now - (lastSweep.get(connection) || 0) < SWEEP_MS) return;
    lastSweep.set(connection, now);
    const known = currentUrls();
    if (known) {
      counters.retiredTransient += sweepTransient(connection, known, now);
      const result = sweep(connection, known, now, stateAllowsRetirement(state));
      counters.retiredRequests += result.retired; counters.retiredRoutes += result.retiredRoutes;
      if (result.skipped) counters.skippedSweeps++;
    } else counters.skippedSweeps++;
    counters.retiredFrames += sweepDetachedFrames(connection, now, activeGuids(connection));
    save();
  }
  const prototype = core.server.DispatcherConnection.prototype;
  const register = prototype.registerDispatcher, dispatch = prototype.dispatch;
  prototype.registerDispatcher = function (d) {
    const result = register.call(this, d);
    try { compact(d); } catch { counters.maintenanceErrors++; }
    return result;
  };
  prototype.dispatch = async function (message) {
    try { return await dispatch.call(this, message); }
    finally { try { maintain(this); } catch { counters.maintenanceErrors++; } }
  };
  const installed = {state, bundle, report, maintain};
  globalThis[KEY] = installed;
  for (const connection of existingConnections) {
    for (const d of connection._dispatcherByGuid.values()) compact(d);
    maintain(connection, true);
  }
  save();
  return installed;
}

module.exports = {install, businessUrls, stateAllowsRetirement, compactRequest, eligibleRequest, sweep, sweepRoutes,
  sweepTransient, sweepDetachedFrames, transientRequest, SDK_SHA, KEEP, MIN_AGE_MS, SWEEP_MS, SETTLED_AGE_MS};

// Loaded with NODE_OPTIONS=--require=<this file> (set by pmos.memory before the
// Python binding starts its driver); only the driver process installs it.
const state = process.env.PMOS_DRIVER_STATE;
if (state && process.argv[2] === 'run-driver') {
  install({state, bundle: path.join(path.dirname(process.argv[1]), 'lib/coreBundle.js')});
}
