export type EvaluationLocation = {
  tab: 'cases' | 'experiments' | 'compare' | 'imports' | 'blind';
  left?: string;
  right?: string;
  experiment?: string;
  caseId?: string;
  repetition?: number;
} & ScenarioLocation;

export type ScenarioLocation = {scenario?: string; scenarioIntent?: string; metric?: string};
export const clearScenario: ScenarioLocation = {scenario: undefined, scenarioIntent: undefined, metric: undefined};

const keys = ['eval', 'eval_left', 'eval_right', 'eval_experiment', 'eval_case', 'eval_repeat', 'eval_scenario', 'eval_intent', 'eval_metric'];
const identifier = (value: string | null) => value && /^[\w.-]{1,128}$/.test(value) ? value : undefined;

export function readEvaluationLocation(search: string): EvaluationLocation {
  const params = new URLSearchParams(search);
  const tab = params.get('eval');
  const experiment = tab === 'blind' ? undefined : identifier(params.get('eval_experiment'));
  const caseId = identifier(params.get('eval_case'));
  const repetition = Number(params.get('eval_repeat'));
  const hasResult = experiment && caseId && Number.isSafeInteger(repetition) && repetition > 0;
  return {
    tab: tab === 'experiments' || tab === 'compare' || tab === 'imports' || tab === 'blind' ? tab : 'cases',
    left: identifier(params.get('eval_left')),
    right: identifier(params.get('eval_right')),
    experiment,
    caseId: hasResult ? caseId : undefined,
    repetition: hasResult ? repetition : undefined,
    ...(tab === 'experiments' && experiment ? {
      ...readScenario(params, 'scenario', 'eval_scenario'),
      ...readScenario(params, 'scenarioIntent', 'eval_intent'),
      ...readScenario(params, 'metric', 'eval_metric'),
    } : {}),
  };
}

function readScenario(params: URLSearchParams, key: keyof ScenarioLocation, parameter: string): ScenarioLocation {
  const raw = params.get(parameter);
  const value = key === 'metric' ? identifier(raw) : raw && raw.length <= 120 && raw.trim() ? raw : undefined;
  return value ? {[key]: value} : {};
}

export function writeEvaluationLocation(href: string, route: EvaluationLocation) {
  const url = new URL(href);
  keys.forEach((key) => url.searchParams.delete(key));
  url.searchParams.set('eval', route.tab);
  if (route.left) url.searchParams.set('eval_left', route.left);
  if (route.right) url.searchParams.set('eval_right', route.right);
  if (route.tab !== 'blind' && route.experiment) url.searchParams.set('eval_experiment', route.experiment);
  if (route.tab !== 'blind' && route.experiment && route.caseId && route.repetition) {
    url.searchParams.set('eval_case', route.caseId);
    url.searchParams.set('eval_repeat', String(route.repetition));
  }
  if (route.tab === 'experiments' && route.experiment) {
    if (route.scenario) url.searchParams.set('eval_scenario', route.scenario);
    if (route.scenarioIntent) url.searchParams.set('eval_intent', route.scenarioIntent);
    if (route.metric) url.searchParams.set('eval_metric', route.metric);
  }
  return url.href;
}
