// Open one member disclosure menu (marketShowQueryZHC) of the column page through
// the page's own left menu, without the automatic first query its view runs on
// mount. Ported from the Liaoning collector (lncrawler/local_member_adaptation.py
// PREPARE/READY); that view's mount logic is not run, as there.
// args: {source: menu infoCode, guid: menu guid, market: the market this account sees}
async args => {
  const NAME = 'marketShowQueryZHC';
  const all = () => {
    const seen = new Set(), found = [];
    const add = v => { if (!v || seen.has(v)) return; seen.add(v); found.push(v); (v.$children || []).forEach(add); };
    document.querySelectorAll('*').forEach(e => add(e.__vue__));
    return found;
  };
  const named = name => all().find(v => v.$options && v.$options.name === name);
  const view = () => all().find(v => v.$options && v.$options.name === NAME && v.infoCode === args.source);
  let vm = view();
  if (!vm) {
    const tree = named('treeMainOvert'), menu = named('chAside'), header = named('chHeader');
    if (!tree || !menu || !header || !header.menuBoxObj) throw new Error('column_shell_missing');
    if (tree.marketInfoTwo !== args.market) throw new Error('column_market_changed');
    let c = tree.$options.components[NAME];
    if (!c) throw new Error('column_view_unknown');
    c = c.resolved || (typeof c === 'function' && !c.options ? await c() : c);
    c = c.default || c.options || c; c = c.options || c;
    if (!c.__pmosControlled) {
      if (c.name !== NAME || !c.methods || !c.methods.queryData || !c.methods.getIpc) throw new Error('column_view_changed');
      c.mounted = []; c.watch = {}; c.__pmosControlled = true;
    }
    const rows = [];
    const walk = nodes => { for (const n of nodes || []) { rows.push(n); walk(n.children || n.childList || []); } };
    walk(menu.decryData(JSON.parse(JSON.stringify(Object.values(header.menuBoxObj).flat()))));
    const nodes = rows.filter(n => n.infoCode === args.source && n.guid === args.guid && n.viewname === NAME);
    if (nodes.length !== 1) throw new Error('column_menu_not_visible');
    menu.handleNodeClick(nodes[0], menu.$refs.tree && menu.$refs.tree.getNode(nodes[0].guid));
    const deadline = Date.now() + 15000;
    while (!(vm = view())) {
      if (Date.now() > deadline) throw new Error('column_view_not_mounted');
      await new Promise(resolve => setTimeout(resolve, 250));
    }
  }
  if (vm.__pmosReady !== args.source) {
    if (vm.marketInfoTwo && vm.marketInfoTwo !== args.market) throw new Error('column_market_changed');
    const data = vm.queryParam.data;
    data.marketId = vm.marketInfoTwo || args.market;
    data.marketCode = vm.loginMarketId === 'PSGCC' ? '' : data.marketId;
    data.memberType = ''; data.memberName = ''; data.infoCode = []; data.time = []; data.generatorType = [];
    vm.pageObj = {pageNum: 1, pageSize: 20};
    vm.resultValue = []; vm.total = 0; vm.tabs = []; vm.fatherCode = '';
    vm.__pmosReady = args.source;
  }
  return true;
}
