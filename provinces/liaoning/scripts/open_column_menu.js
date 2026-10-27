// Open one disclosure menu of the column page (columnHomeLeftMenuNew)
// through the page's own left menu, without the automatic first query its view
// runs on mount. Ported from the Liaoning collector
// (lncrawler/local_document_adaptation.py PREPARE/READY).
// args: {component: menu viewname, source: menu infoCode, guid: menu guid}
async args => {
  const all = () => {
    const seen = new Set(), found = [];
    const add = v => { if (!v || seen.has(v)) return; seen.add(v); found.push(v); (v.$children || []).forEach(add); };
    document.querySelectorAll('*').forEach(e => add(e.__vue__));
    return found;
  };
  const named = name => all().find(v => v.$options && v.$options.name === name);
  const view = () => all().find(v => v.$options && v.$options.name === args.component
    && v.infoCode === args.source && v.viewId === args.guid);
  let vm = view();
  if (!vm) {
    const tree = named('treeMainOvert'), menu = named('chAside'), header = named('chHeader');
    if (!tree || !menu || !header) throw new Error('column_shell_missing');
    let c = tree.$options.components[args.component];
    if (!c) throw new Error('column_view_unknown');
    c = c.resolved || (typeof c === 'function' && !c.options ? await c() : c);
    c = c.default || c.options || c; c = c.options || c;
    if (!c.__pmosMounted) {
      if (c.name !== args.component || !c.methods || !c.methods.queryData) throw new Error('column_view_changed');
      c.__pmosMounted = Array.isArray(c.mounted) ? c.mounted : (c.mounted ? [c.mounted] : []);
      c.mounted = []; c.watch = {};
    }
    const rows = [];
    const walk = nodes => { for (const n of nodes || []) { rows.push(n); walk(n.children || n.childList || []); } };
    walk(menu.decryData(JSON.parse(JSON.stringify(Object.values(header.menuBoxObj).flat()))));
    const node = rows.find(n => n.infoCode === args.source && n.guid === args.guid && n.viewname === args.component);
    if (!node) throw new Error('column_menu_not_visible');
    menu.handleNodeClick(node, menu.$refs.tree && menu.$refs.tree.getNode(node.guid));
    const deadline = Date.now() + 15000;
    while (!(vm = view())) {
      if (Date.now() > deadline) throw new Error('column_view_not_mounted');
      await new Promise(resolve => setTimeout(resolve, 250));
    }
  }
  if (vm.__pmosReady !== args.source) {
    // Run the view's own mount logic once, minus getIpc (its first query).
    const original = vm.getIpc;
    if (typeof original === 'function') vm.getIpc = () => {};
    try {
      for (const hook of vm.$options.__pmosMounted || []) await hook.call(vm);
      await vm.$nextTick();
    } finally {
      if (typeof original === 'function') vm.getIpc = original;
    }
    vm.resultValue = []; vm.total = 0;
    if (vm.ipcMap && typeof vm.ipcMap.clear === 'function') vm.ipcMap.clear();
    if (vm.queryParam && vm.queryParam.data) vm.queryParam.data.menuId = vm.viewId;
    if (vm.queryParam && vm.queryParam.pageInfo) vm.queryParam.pageInfo.pageNum = 1;
    vm.__pmosReady = args.source;
  }
  return true;
}
