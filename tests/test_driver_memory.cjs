'use strict';
// Real installed Playwright SDK (TEST_PLAYWRIGHT_BUNDLE); offline, no browser or
// HTTP server. Ported from Shanxi production's driver lifecycle tests.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), os = require('node:os');
const patch = require('../pmos/browser/driver_memory.cjs');
const bundle = path.resolve(process.env.TEST_PLAYWRIGHT_BUNDLE);
const core = require(bundle), S = core.server;
const URL = 'https://pmos.sx.sgcc.com.cn/px-settlement-infpubquery/ipDisclosureApply/getData';
const urls = new Set([URL]);
const state = fs.mkdtempSync(path.join(os.tmpdir(), 'pmos-driver-test-'));
const write = (name, value) => fs.writeFileSync(path.join(state, name + '.json'), JSON.stringify(value));
function reset() {
  for (const n of fs.readdirSync(state)) fs.unlinkSync(path.join(state, n));
  write('status', {pid: process.ppid, phase: 'waiting', updated_at: new Date().toISOString()});
  write('driver_business_urls', {urls: [URL]});
}
function connection() {
  const c = new S.DispatcherConnection(false), messages = [];
  c.onmessage = m => messages.push({method: m.method, guid: m.guid, reason: m.params?.reason});
  return {c, root: new S.RootDispatcher(c, async () => {}), messages};
}
const TIMING = Object.fromEntries(['startTime', 'domainLookupStart', 'domainLookupEnd', 'connectStart',
  'secureConnectionStart', 'connectEnd', 'requestStart', 'responseStart'].map(k => [k, 0]));
function request(f, options = {}) {
  const body = Buffer.alloc(65536, 7).subarray(0, 32);
  const parent = {attribution: f.root._object.attribution, instrumentation: f.root._object.instrumentation};
  const r = new S.Request(parent, null, null, null, null, options.url || URL, 'xhr', 'POST', body, [], Date.now() - (options.age ?? 300000));
  const response = new S.Response(r, options.status || 200, 'OK', [], TIMING, async () => Buffer.from('offline'), false);
  if (!options.pending) response._requestFinished(2);
  if (options.failed) r._failureText = 'offline_failure';
  const d = S.RequestDispatcher.from(f.root, r);
  return {d, r, response};
}
const count = c => [...c._dispatcherByGuid.values()].filter(d => d._type === 'Request').length;
test.after(() => fs.rmSync(state, {recursive: true, force: true}));

test('business URL list must be present and well formed', () => {
  reset();
  assert.deepEqual([...patch.businessUrls(state)], [URL]);
  write('driver_business_urls', {urls: ['javascript:alert(1)']});
  assert.throws(() => patch.businessUrls(state), /no_business_urls/);
});

test('private copy keeps exact bytes and request semantics, drops the 64 KiB backing', () => {
  const f = connection(), {d, r} = request(f), prior = Buffer.from(r.postDataBuffer());
  assert.equal(r._postData.buffer.byteLength, 65536);
  assert.deepEqual(patch.compactRequest(d, urls), {bodyBytes: 32, priorBackingBytes: 65536});
  assert.deepEqual(r.postDataBuffer(), prior); assert.equal(r._postData.buffer.byteLength, 32);
  assert.equal(r.method(), 'POST'); assert.equal(r.url(), URL); assert.equal(d._disposed, false);
  assert.equal(patch.compactRequest(d, urls), null);
});

test('latest 128 and pending, failed, recent, HTTP errors, active request/response are kept', () => {
  const f = connection(), kept = [request(f, {pending: true}), request(f, {failed: true}), request(f, {age: 0}),
    request(f, {status: 403}), request(f), request(f)];
  f.c._activeProgressControllers.set('a', {controller: {metadata: {objectId: kept[4].d._guid}}});
  f.c._activeProgressControllers.set('b', {controller: {metadata: {objectId: [...kept[5].d._dispatchers.keys()][0]}}});
  const old = request(f); for (let i = 0; i < 128; i++) request(f);
  assert.equal(patch.sweep(f.c, urls, Date.now(), true).retired, 1);
  assert.equal(old.d._disposed, true); assert.ok(kept.every(x => !x.d._disposed));
  assert.equal(count(f.c), 134); assert.equal(f.root._disposed, false);
  assert.equal(f.messages.find(m => m.method === '__dispose__').reason, 'gc');
});

test('nothing of the business is retired while the state does not allow it', () => {
  const f = connection(); for (let i = 0; i < 200; i++) request(f);
  assert.equal(patch.sweep(f.c, urls, Date.now(), false).skipped, true);
  assert.equal(count(f.c), 200);
});

test('state gate: owner pid, fresh status, no block', () => {
  reset(); assert.equal(patch.stateAllowsRetirement(state), true);
  write('block', {reason: 'preserve'}); assert.equal(patch.stateAllowsRetirement(state), false);
  reset(); write('status', {pid: -1, updated_at: new Date().toISOString()}); assert.equal(patch.stateAllowsRetirement(state), false);
  reset(); write('status', {pid: process.ppid, updated_at: '2020-01-01T00:00:00+08:00'}); assert.equal(patch.stateAllowsRetirement(state), false);
  reset(); fs.writeFileSync(path.join(state, 'status.json'), '{'); assert.equal(patch.stateAllowsRetirement(state), false);
});

test('invalid SDK or connection is refused before any hook changes', () => {
  reset(); const original = S.DispatcherConnection.prototype.registerDispatcher;
  const bad = path.join(state, 'bad.cjs'); fs.writeFileSync(bad, 'module.exports={}');
  assert.throws(() => patch.install({state, bundle: bad}), /unreviewed_playwright_driver/);
  assert.throws(() => patch.install({state, bundle, existingConnections: [{}]}), /foreign_driver_connection/);
  assert.equal(S.DispatcherConnection.prototype.registerDispatcher, original);
});

// Page traffic around the gate's route('**/*') windows.
const SdkObject = Object.getPrototypeOf(S.Request), BaseDispatcher = Object.getPrototypeOf(S.RequestDispatcher);
function pageRequest(f, {url = 'https://pmos.sx.sgcc.com.cn/pxf-settlement-outnetpub/js/app.js', type = 'script', method = 'GET',
                         age = 300000, finished = true, failure = null, route = true, handled = true} = {}) {
  const parent = {attribution: f.root._object.attribution, instrumentation: f.root._object.instrumentation};
  const r = new S.Request(parent, null, null, null, null, url, type, method, null, [], Date.now() - age);
  if (failure) r._failureText = failure;
  else { const response = new S.Response(r, 200, 'OK', [], TIMING, async () => Buffer.from('offline'), false); if (finished) response._requestFinished(2); }
  const d = S.RequestDispatcher.from(f.root, r);
  let routeDispatcher = null;
  if (route) { routeDispatcher = new BaseDispatcher(d, new SdkObject(r, 'route'), 'Route', {request: d}); routeDispatcher._handled = handled; }
  return {d, r, route: routeDispatcher};
}
const BLOCKED = 'net::ERR_BLOCKED_BY_CLIENT';

test('settled static and gate-blocked page traffic is retired with its route; the rest stays', () => {
  const f = connection();
  const staticOld = pageRequest(f), blockedOld = pageRequest(f, {type: 'xhr', method: 'POST', url: 'https://pmos.sx.sgcc.com.cn/px-x/extra', finished: false, failure: BLOCKED});
  const kept = [
    pageRequest(f, {finished: false}),
    pageRequest(f, {handled: false}),
    pageRequest(f, {type: 'xhr', method: 'POST', url: 'https://pmos.sx.sgcc.com.cn/px-common-authcenter/sso/token', age: 200000}),
    pageRequest(f, {type: 'xhr', method: 'POST', finished: false, failure: 'net::ERR_CONNECTION_RESET', age: 200000}),
    pageRequest(f, {url: URL, type: 'xhr', method: 'POST'}),
    pageRequest(f, {age: 0}),
    pageRequest(f),
  ];
  f.c._activeProgressControllers.set('busy', {controller: {metadata: {objectId: kept[6].route._guid}}});
  const apiSettled = pageRequest(f, {type: 'xhr', method: 'POST', url: 'https://pmos.sx.sgcc.com.cn/px-common-authcenter/sso/token', age: patch.SETTLED_AGE_MS});
  for (let i = 0; i < 128; i++) pageRequest(f);
  assert.equal(patch.sweepTransient(f.c, urls, Date.now()), 3);
  assert.ok([staticOld, blockedOld, apiSettled].every(x => x.d._disposed && x.route._disposed));
  assert.ok(kept.every(x => !x.d._disposed));
  assert.equal(patch.sweepTransient(f.c, urls, Date.now()), 0, 'the newest 128 page requests always stay');
});

test('a finished business route is retired once its request is settled and old', () => {
  const f = connection();
  const make = () => {
    const x = request(f), r = x.r, ctx = {_routesInFlight: new Set()};
    r._context = ctx;
    const routeObject = Object.assign(new SdkObject(r, 'route'), {_handled: true, _request: r, _currentHandler: undefined, _futureHandlers: []});
    const route = new BaseDispatcher(x.d, routeObject, 'Route', {request: x.d}); route._handled = true;
    return {...x, route, ctx};
  };
  const inFlight = make(); inFlight.ctx._routesInFlight.add(inFlight.route._object);
  const done = make();
  for (let i = 0; i < 128; i++) request(f);
  const result = patch.sweep(f.c, urls, Date.now(), true);
  assert.equal(result.retiredRoutes, 1); assert.equal(done.route._disposed, true); assert.equal(done.d._disposed, true);
  assert.equal(inFlight.route._disposed, false); assert.equal(inFlight.d._disposed, false);
});

test('installed module bounds repeated gate windows and writes counters', () => {
  reset();
  const f = connection(); const inst = patch.install({state, bundle, existingConnections: [f.c]});
  const pageCount = () => [...f.c._dispatcherByGuid.values()].filter(d => d._type === 'Request' || d._type === 'Route').length;
  for (let round = 0; round < 20; round++) {
    for (let i = 0; i < 100; i++) pageRequest(f, {type: i % 3 ? 'script' : 'image', failure: i % 5 ? null : BLOCKED, finished: !!(i % 5)});
    for (let i = 0; i < 20; i++) request(f);
    inst.maintain(f.c, true);
    assert.ok(pageCount() <= 4 * 128, `round ${round}: ${pageCount()}`);
  }
  const report = JSON.parse(fs.readFileSync(path.join(state, 'driver_memory.json'), 'utf8'));
  assert.equal(report.maintenanceErrors, 0); assert.equal(report.createdTimers, 0); assert.equal(report.businessUrls, 1);
  assert.ok(report.retiredTransient > 1500 && report.retiredRequests > 200 && report.copiedBodies >= 400);
  assert.equal(patch.install({state, bundle}), inst);
  assert.equal(f.root._disposed, false);
});

test('detached iframes are released only after five settled minutes and without children', () => {
  const f = connection(), frames = new Map(), page = {frameManager: {_frames: frames}};
  const make = (id, attached) => { const o = new SdkObject(f.root._object, 'frame'); o._id = id; o._page = page; if (attached) frames.set(id, o);
    return new BaseDispatcher(f.root, o, 'Frame', {url: '', name: '', loadStates: []}, 'Frame'); };
  const attached = make('a', true), detached = make('b', false), withChild = make('c', false);
  new BaseDispatcher(withChild, new SdkObject(f.root._object, 'handle'), 'JSHandle', {preview: ''});
  const t0 = Date.now();
  assert.equal(patch.sweepDetachedFrames(f.c, t0, new Set()), 0);
  assert.equal(patch.sweepDetachedFrames(f.c, t0 + patch.SETTLED_AGE_MS - 1, new Set()), 0);
  assert.equal(patch.sweepDetachedFrames(f.c, t0 + patch.SETTLED_AGE_MS, new Set()), 1);
  assert.ok(detached._disposed && !attached._disposed && !withChild._disposed);
});
