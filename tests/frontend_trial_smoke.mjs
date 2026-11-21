// 前端试算面板渲染冒烟测试：用最小 DOM 桩加载 app.js 中的渲染函数。
// 运行：node tests/frontend_trial_smoke.mjs
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const appSrc = readFileSync(new URL('../frontend/src/app.js', import.meta.url), 'utf8');
const commonSrc = readFileSync(new URL('../frontend/src/components/common.js', import.meta.url), 'utf8');
const stateSrc = readFileSync(new URL('../frontend/src/stores/app-state.js', import.meta.url), 'utf8');

// 抽取 app.js 里需要的纯函数（不触达 DOM/网络）
function extract(src, name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`未找到函数 ${name}`);
  let depth = 0, i = src.indexOf('{', start);
  const begin = i;
  for (; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') { depth--; if (depth === 0) break; }
  }
  return src.slice(start, i + 1);
}

const sandbox = {};
sandbox.window = sandbox;
sandbox.localStorage = {getItem: () => null, setItem() {}, removeItem() {}};
sandbox.document = {querySelector: () => null};
sandbox.console = console;
vm.createContext(sandbox);
// 词法声明（const state/function）需在同一脚本作用域内共享，因此拼接后一次执行
vm.runInContext([
  commonSrc.replace(/export /g, ''),
  stateSrc.replace(/export /g, ''),
  'globalThis.__state = state;',
  'state.user = {role: "supervisor"}; state.page = "atlas";',
  extract(appSrc, 'stat'),
  extract(appSrc, 'mapSvg'),
  extract(appSrc, 'trialPanel'),
  extract(appSrc, 'atlas'),
].join('\n'), sandbox);

const mkPoint = (i, qs = 'checked') => ({
  id: i, point_code: `SP-1121-${String(i).padStart(2, '0')}`, manhole_id: i,
  asset_code: `MH-${String(i).padStart(3, '0')}`, street_name: '南河路',
  x: 1010 + i, y: 1980 + i, elevation_mm: 100, settlement_mm: 12,
  coordinate_reference: 'WGS84-METER', measured_at: '2026-11-21T07:30:00Z',
  quality_status: qs, cluster_label: null, local_density: null,
});
const data = {
  user: {role: 'supervisor'},
  cluster_setting: {eps_m: 26, min_samples: 3, updated_at: '2026-11-21T07:00:00Z', updated_by: 1},
  batches: [{id: 1, batch_code: 'SURV-2026-1121-AM', measured_at: '2026-11-21T07:30:00Z', instrument: 'Leica', coordinate_reference: 'WGS84-METER', status: 'ready', notes: '', point_count: 11}],
  points: Array.from({length: 11}, (_, k) => mkPoint(k + 1)),
  zones: [], notices: [], resurveys: [], jobs: [],
  stats: {assets: 11, points: 11, zones: 0, open_zones: 0, notices: 0, resurveys: 0},
  parameter_trials: [
    {
      id: 1, trial_code: 'TRIAL-1121AM-001-ab12', batch_id: 1, status: 'open',
      baseline_eps_m: 26, baseline_min_samples: 3, candidate_eps_m: 20, candidate_min_samples: 4,
      point_snapshot_hash: 'h1', stale_reason: null, approved_by: null, approved_at: null,
      created_at: '2026-11-21T08:00:00Z', created_by: 2,
      report: {
        point_count: 11,
        baseline: {params: {}, cluster_count: 2, noise_count: 1, clusters: []},
        candidate: {params: {}, cluster_count: 1, noise_count: 4, clusters: []},
        diff: {
          hot_to_scattered_count: 3, scattered_to_hot_count: 0, changed_membership_count: 1,
          hot_to_scattered: [
            {point_id: 4, point_code: 'SP-1121-04', manhole_id: 4, baseline_label: 0, candidate_label: null, settlement_mm: 12.1},
            {point_id: 5, point_code: 'SP-1121-05', manhole_id: 5, baseline_label: 0, candidate_label: null, settlement_mm: 13.4},
            {point_id: 9, point_code: 'SP-1121-09', manhole_id: 9, baseline_label: 1, candidate_label: null, settlement_mm: 15.2},
          ],
          scattered_to_hot: [], changed_membership: [{point_id: 2, point_code: 'SP-1121-02'}],
        },
      },
    },
    {
      id: 2, trial_code: 'TRIAL-1121AM-002-cd34', batch_id: 1, status: 'stale',
      baseline_eps_m: 26, baseline_min_samples: 3, candidate_eps_m: 18, candidate_min_samples: 4,
      point_snapshot_hash: 'h0', stale_reason: '测量点 SP-1121-11 新增，测点集合已变化，对照报告过期需重算',
      approved_by: null, approved_at: null, created_at: '2026-11-21T07:50:00Z', created_by: 2,
      report: {baseline: {cluster_count: 2, noise_count: 1}, candidate: {cluster_count: 1, noise_count: 3},
               diff: {hot_to_scattered_count: 2, scattered_to_hot_count: 0, changed_membership_count: 0,
                      hot_to_scattered: [{point_code: 'SP-1121-03'}, {point_code: 'SP-1121-07'}], scattered_to_hot: [], changed_membership: []}},
    },
    {
      id: 3, trial_code: 'TRIAL-1121AM-003-ef56', batch_id: 1, status: 'approved',
      baseline_eps_m: 30, baseline_min_samples: 2, candidate_eps_m: 26, candidate_min_samples: 3,
      point_snapshot_hash: 'h-1', stale_reason: null, approved_by: 2, approved_at: '2026-11-20T17:00:00Z',
      created_at: '2026-11-20T16:50:00Z', created_by: 2,
      report: {baseline: {cluster_count: 3, noise_count: 0}, candidate: {cluster_count: 2, noise_count: 1},
               diff: {hot_to_scattered_count: 0, scattered_to_hot_count: 0, changed_membership_count: 0,
                      hot_to_scattered: [], scattered_to_hot: [], changed_membership: []}},
    },
  ],
};

const results = [];
const check = (name, cond) => { results.push([name, !!cond]); };

const html = sandbox.trialPanel(data);
check('渲染候选参数表单 eps/min_samples', html.includes('name="eps_m"') && html.includes('name="min_samples"'));
check('显示现行默认 26/3', html.includes('eps 26m') && html.includes('min_samples 3'));
check('展示三张报告卡片', (html.match(/class="trial-card /g) || []).length === 3);
check('热区->散点数量 3', html.includes('<strong>3</strong>'));
check('标出三个井盖编号', ['SP-1121-04', 'SP-1121-05', 'SP-1121-09'].every(c => html.includes(c)));
check('井盖标签使用 lost 样式', (html.match(/manhole-tag lost/g) || []).length >= 3);
check('待签批报告有签批按钮', html.includes('data-action="approve-trial"') && html.includes('data-id="1"'));
check('过期报告有重算按钮', html.includes('data-action="retrial"') && html.includes('data-id="2"'));
check('过期报告展示过期原因', html.includes('测点集合已变化'));
check('已签批报告展示签批信息', html.includes('已成为现行默认值'));
check('已签批报告无重算/签批按钮', !html.includes('data-id="3"'));
check('批次显示编号而非裸 id', html.includes('SURV-2026-1121-AM'));
check('发起试算表单带批次 id', html.includes('id="trial-form"') && html.includes('data-batch-id="1"'));
check('发起按钮为 submit 类型', html.includes('type="submit"'));

// viewer 角色：操作按钮应隐藏
const viewerData = JSON.parse(JSON.stringify(data));
sandbox.__state.user = {role: 'viewer'};
const viewerHtml = sandbox.trialPanel(viewerData);
check('viewer 看到禁用的发起按钮', viewerHtml.includes('仅测绘主管可发起试算'));
check('viewer 看不到签批按钮', !viewerHtml.includes('approve-trial'));
check('viewer 看到只读提示', viewerHtml.includes('仅测绘主管可签批 / 重算'));

// 基准冲突时发起按钮禁用
sandbox.__state.user = {role: 'supervisor'};
const conflictData = JSON.parse(JSON.stringify(data));
conflictData.points[10].quality_status = 'datum_conflict';
const conflictHtml = sandbox.trialPanel(conflictData);
check('基准冲突时发起按钮禁用', conflictHtml.includes('disabled') && conflictHtml.includes('先统一基准再试算'));

// atlas 页面整体包含试算面板且小参数行用最新默认值
const atlasHtml = sandbox.atlas(data);
check('测绘页包含试算面板', atlasHtml.includes('默认聚类参数调参对照'));
check('聚类按钮参数行使用动态默认值', atlasHtml.includes('eps 26m · min_samples 3'));

let failed = 0;
for (const [name, ok] of results) {
  console.log((ok ? '  ✓ ' : '  ✗ ') + name);
  if (!ok) failed++;
}
console.log(`\n通过 ${results.length - failed} 项，失败 ${failed} 项`);
process.exit(failed ? 1 : 0);
