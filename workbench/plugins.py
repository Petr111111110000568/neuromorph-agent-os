"""Curated, dimensionless research demonstrations; no biological interventions."""
import math
import random
import statistics
from collections import Counter
from .kan import SPEC as KAN_SPEC
from .cortical import SPEC as CORTICAL_SPEC


def number(title,default,low,high,integer=False):
    return {'title':title,'type':'integer' if integer else 'number','default':default,'minimum':low,'maximum':high}

SPECS=[
 {'id':'structural_plasticity','name':'Структурная пластичность: эксперимент','version':'0.7.0',
  'description':'Синтетическое сравнение фиксированной топологии, случайного роста и роста связей по тренировочному градиенту при смене зависимости.',
  'parameters':{'type':'object','additionalProperties':False,'properties':{
   'steps_per_phase':number('Шаги в каждой фазе',120,60,180,True),
   'replicates':number('Последовательные seed',3,1,5,True),
   'edge_budget':number('Рёбра вход→скрытый слой',18,8,30,True),
   'rewiring_interval':number('Интервал перестройки',20,10,40,True),
   'learning_rate':number('Скорость обучения',.05,.005,.12),
   'weight_decay':number('Затухание весов',.0005,0,.02),
   'seed':number('Начальный seed',42,0,2147483647,True)}},
  'limitations':['Наша инженерная модель; не алгоритм С. В. Савельева и не модель мозга или морфогенеза организма.',
                'Перестраиваются только связи при фиксированном числе узлов; нет геномов, клеток и биологических вмешательств.',
                'Стоимость измеряется условными посещениями рёбер, включая оценку кандидатов и тестирование; это не энергия в джоулях.',
                'Ошибка на одной синтетической задаче и нескольких seed не доказывает универсального преимущества метода.']},
 {'id':'coupled_dynamics','name':'Динамика связанных систем','description':'Сравнение базовой и возмущённой трёхкомпонентной модели с вариацией параметров.',
  'parameters':{'type':'object','additionalProperties':False,'properties':{
   'steps':number('Шаги расчёта',240,20,1000,True),'recovery':number('Восстановление',.6,.1,2),
   'coupling':number('Связь компонентов',.25,0,1),'load':number('Внешняя нагрузка',.2,0,1),
   'perturbation':number('Возмущение',.5,0,2),'replicates':number('Повторы',24,3,50,True),
   'uncertainty':number('Разброс параметров',.15,0,.4),'seed':number('Seed',42,0,2147483647,True)}},
  'limitations':['Абстрактные безразмерные переменные; модель не калибрована на организме.','Интервал отражает заданный разброс параметров, а не вероятность клинического исхода.']},
 {'id':'regression_benchmark','name':'ML: проверка на отложенных данных','description':'Линейная и RBF-регрессия на синтетическом сигнале; сравнение ошибки на независимой выборке.',
  'parameters':{'type':'object','additionalProperties':False,'properties':{
   'samples':number('Число наблюдений',180,60,500,True),'basis_count':number('RBF-функции',12,3,24,True),
   'noise':number('Шум наблюдений',.08,0,.5),'ridge':number('Регуляризация',.01,.000001,1),
   'seed':number('Seed',42,0,2147483647,True)}},
  'limitations':['Синтетическая задача аппроксимации; качество на ней не переносится автоматически на геномику.','RBF-регрессия не является реализацией FastKAN или Wav-KAN.']},
 {'id':'quantum_circuit','name':'Квантовая схема: классическая симуляция','description':'Точное распределение двух кубитов после Ry и CNOT, затем статистическая выборка измерений.',
  'parameters':{'type':'object','additionalProperties':False,'properties':{
   'theta':number('Угол Ry (радианы)',math.pi/2,0,math.pi),'depolarizing':number('Доля деполяризации',0,0,1),
   'shots':number('Измерения',2000,100,20000,True),'seed':number('Seed',42,0,2147483647,True)}},
  'limitations':['Вычисляется на классическом процессоре; QPU не подключён.','Не демонстрирует квантовое ускорение биологических исследований.']},
 {'id':'legacy_topology','name':'Проверка контракта топологии v0.3','description':'Проверяет полноту абстрактного описания зависимости системы; сохраняет совместимость с архивом v0.3.',
  'parameters':{'type':'object','additionalProperties':False,'properties':{
   'system_id':{'title':'Идентификатор узла','type':'string','default':'model_component','minLength':1,'maxLength':80},
   'inputs':{'title':'Входы через запятую','type':'string','default':'energy,information','maxLength':500},
   'outputs':{'title':'Выходы через запятую','type':'string','default':'state','maxLength':500},
   'dependencies':{'title':'Зависимости через запятую','type':'string','default':'transport,regulation','maxLength':500}}},
  'limitations':['Проверяется описание графа, а не возможность создать орган.','Эвристические числа исходного ConsequenceEngine v0.3 не используются как биологические прогнозы.']},
]
SPECS.extend([KAN_SPEC, CORTICAL_SPEC])


def list_plugins():
    import copy
    return [{**copy.deepcopy(p),'version':p.get('version','0.4.0'),'status':'ready','kind':'builtin','trust':'curated'} for p in SPECS]


def validate(plugin_id,parameters):
    spec=next((x for x in SPECS if x['id']==plugin_id),None)
    if not spec:raise ValueError('Неизвестный плагин')
    if not isinstance(parameters,dict):raise ValueError('parameters должен быть объектом')
    props=spec['parameters']['properties']
    if set(parameters)-set(props):raise ValueError('Неизвестные параметры')
    result={}
    for key,schema in props.items():
        value=parameters.get(key,schema.get('default'))
        typ=schema['type']
        if typ=='string':
            if not isinstance(value,str) or len(value)<schema.get('minLength',0) or len(value)>schema.get('maxLength',1000):
                raise ValueError(f'{key}: недопустимый текст')
        else:
            if isinstance(value,bool) or not isinstance(value,(int,float)) or (isinstance(value,float) and not math.isfinite(value)):
                raise ValueError(f'{key}: требуется конечное число')
            if typ=='integer' and not isinstance(value,int):raise ValueError(f'{key}: требуется целое число')
            if not schema['minimum']<=value<=schema['maximum']:raise ValueError(f'{key}: значение вне диапазона')
        if 'enum' in schema and value not in schema['enum']:
            raise ValueError(f'{key}: неподдерживаемое значение')
        result[key]=value
    return result


def percentile(values,p):
    xs=sorted(values); pos=(len(xs)-1)*p; lo=int(pos); hi=math.ceil(pos)
    return xs[lo]+(xs[hi]-xs[lo])*(pos-lo)


def trajectory(p,recovery,coupling,perturbation,component=None):
    # dx_i/dt = -r*x_i + c*(mean(x)-x_i) + forcing_i.
    # Largest Euler spectral factor within permitted ranges: dt*(r+c)<=0.18.
    dt=.05; state=[0.,0.,0.]; points=[]
    for step in range(p['steps']+1):
        points.append(sum(state)/3 if component is None else state[component])
        if step==p['steps']:break
        mean=sum(state)/3
        forcing=[p['load'],.6*p['load'],.3*p['load']]
        if p['steps']//4<=step<p['steps']//2:forcing[0]+=perturbation
        state=[x+dt*(-recovery*x+coupling*(mean-x)+force) for x,force in zip(state,forcing)]
    return points


def coupled(p):
    rng=random.Random(p['seed']); deltas=[]
    for _ in range(p['replicates']):
        r=p['recovery']*(1+rng.uniform(-p['uncertainty'],p['uncertainty']))
        c=p['coupling']*(1+rng.uniform(-p['uncertainty'],p['uncertainty']))
        base=trajectory(p,r,c,0); changed=trajectory(p,r,c,p['perturbation'])
        deltas.append([a-b for a,b in zip(changed,base)])
    baseline=trajectory(p,p['recovery'],p['coupling'],0)
    changed=trajectory(p,p['recovery'],p['coupling'],p['perturbation'])
    series=[{'name':'Базовая модель','points':[{'x':i*.05,'y':y} for i,y in enumerate(baseline)]},
            {'name':'Модель с возмущением','points':[{'x':i*.05,'y':y} for i,y in enumerate(changed)]}]
    local_base=trajectory(p,p['recovery'],p['coupling'],0,component=0)
    local_changed=trajectory(p,p['recovery'],p['coupling'],p['perturbation'],component=0)
    series.extend([
        {'name':'Компонент 1 · базовый','points':[{'x':i*.05,'y':y} for i,y in enumerate(local_base)]},
        {'name':'Компонент 1 · возмущённый','points':[{'x':i*.05,'y':y} for i,y in enumerate(local_changed)]}])
    for name,percent in [('Разность: 5-й процентиль',.05),('Разность: 95-й процентиль',.95)]:
        series.append({'name':name,'points':[{'x':i*.05,'y':percentile([d[i] for d in deltas],percent)} for i in range(p['steps']+1)]})
    areas=[sum(d)*.05 for d in deltas]; end=[d[-1] for d in deltas]
    return {'summary':'Рассчитано воздействие абстрактного импульса на линейную связанную модель. Изменения параметров и численная модель сохранены.',
      'metrics':[{'label':'Средний интеграл разности','value':statistics.mean(areas),'unit':'условные единицы × время'},
                 {'label':'Остаточная разность','value':statistics.mean(end),'unit':'условные единицы'},
                 {'label':'Повторы','value':p['replicates']}],
      'series':series,'table':[{'показатель':'integral_delta','p05':percentile(areas,.05),'p50':percentile(areas,.5),'p95':percentile(areas,.95)},
                             {'показатель':'final_delta','p05':percentile(end,.05),'p50':percentile(end,.5),'p95':percentile(end,.95)}],
      'model':{'equation':'dx_i/dt = -r*x_i + c*(mean(x)-x_i) + forcing_i','dt':.05,'calibrated':False,'units':'dimensionless',
               'mean_invariant':'Диффузионная связь меняет отдельные компоненты, но её сумма равна нулю: среднее не зависит от coupling.'}}


def solve(matrix,vector):
    a=[row[:]+[val] for row,val in zip(matrix,vector)]; n=len(a)
    for i in range(n):
        k=max(range(i,n),key=lambda j:abs(a[j][i]));a[i],a[k]=a[k],a[i]
        if abs(a[i][i])<1e-14:raise ValueError('Вырожденная система')
        scale=a[i][i];a[i]=[x/scale for x in a[i]]
        for j in range(n):
            if i!=j:
                scale=a[j][i];a[j]=[x-scale*y for x,y in zip(a[j],a[i])]
    return [row[-1] for row in a]


def fit(features,ys,ridge):
    n=len(features[0]); matrix=[[sum(row[i]*row[j] for row in features)+(ridge if i==j else 0) for j in range(n)] for i in range(n)]
    vector=[sum(row[i]*y for row,y in zip(features,ys)) for i in range(n)]
    return solve(matrix,vector)


def regression(p):
    rng=random.Random(p['seed']);target=lambda x:math.sin(3*x)+.3*x
    samples=[(rng.uniform(-2,2),rng.gauss(0,p['noise'])) for _ in range(p['samples'])]
    split=int(len(samples)*.7);train=samples[:split];test=samples[split:]
    centers=[-2+4*i/(p['basis_count']-1) for i in range(p['basis_count'])];width=4/(p['basis_count']-1)
    functions={'linear':lambda x:[1.,x],'rbf':lambda x:[1.]+[math.exp(-.5*((x-c)/width)**2) for c in centers]}
    rows=[]; series=[{'name':'Истинная функция','points':[{'x':-2+i*.04,'y':target(-2+i*.04)} for i in range(101)]}]
    for name,features in functions.items():
        weights=fit([features(x) for x,_ in train],[target(x)+e for x,e in train],p['ridge'])
        pred=lambda x:sum(a*b for a,b in zip(features(x),weights))
        train_error=statistics.mean((pred(x)-target(x)-e)**2 for x,e in train)
        test_error=statistics.mean((pred(x)-target(x)-e)**2 for x,e in test)
        rows.append({'model':name,'train_mse':train_error,'test_mse':test_error,'train_n':len(train),'test_n':len(test)})
        series.append({'name':name,'points':[{'x':-2+i*.04,'y':pred(-2+i*.04)} for i in range(101)]})
    return {'summary':'Данные разделены до обучения (70/30). Гиперпараметры фиксированы входным заданием; тестовая выборка не использована для настройки.',
      'metrics':[{'label':'Test MSE · linear','value':rows[0]['test_mse']},{'label':'Test MSE · RBF','value':rows[1]['test_mse']}],
      'table':rows,'series':series,'validation':{'split':'seeded iid holdout','train_n':len(train),'test_n':len(test),'test_used_for_fitting':False}}


def quantum(p):
    c=math.cos(p['theta']/2)**2;s=math.sin(p['theta']/2)**2;noise=p['depolarizing']
    probs=[(1-noise)*c+noise/4,noise/4,noise/4,(1-noise)*s+noise/4]
    states=['00','01','10','11']; rng=random.Random(p['seed']); counts=Counter(rng.choices(states,weights=probs,k=p['shots']))
    rows=[{'state':state,'theory_probability':pr,'counts':counts[state],'observed_frequency':counts[state]/p['shots'],
           'sampling_standard_error':math.sqrt(pr*(1-pr)/p['shots'])} for state,pr in zip(states,probs)]
    entropy=-sum(pr*math.log2(pr) for pr in probs if pr)
    return {'summary':'На CPU рассчитана схема |00⟩ → Ry(θ) на первом кубите → CNOT. Показаны точные вероятности и воспроизводимая выборка измерений.',
      'metrics':[{'label':'Сумма вероятностей','value':sum(probs)},{'label':'Энтропия измерений','value':entropy,'unit':'бит'},{'label':'Измерения','value':p['shots']}],
      'table':rows,'series':[],'model':{'backend':'classical_exact','qubits':2,'qpu_used':False,'noise_model':'global depolarizing mixture'}}


def topology(p):
    def split(value):return list(dict.fromkeys(x.strip() for x in value.split(',') if x.strip()))
    inputs=split(p['inputs']);outputs=split(p['outputs']);deps=split(p['dependencies'])
    problems=[]
    if not inputs:problems.append('Не указаны входы')
    if not outputs:problems.append('Не указаны выходы')
    if len(deps)<2:problems.append('Недостаточно описаны зависимости')
    if p['system_id'] in deps:problems.append('Узел зависит от самого себя')
    return {'summary':'Проверена структурная полнота контракта. Биологическая осуществимость не оценивалась.',
      'metrics':[{'label':'Зависимости','value':len(deps)},{'label':'Замечания','value':len(problems)}],
      'table':[{'type':'input','node':x} for x in inputs]+[{'type':'output','node':x} for x in outputs]+[{'type':'dependency','node':x} for x in deps],
      'series':[],'contract':{'organ_id':p['system_id'],'inputs':inputs,'outputs':outputs,'dependencies':deps,'topology_only':True},
      'findings':problems,'prediction_available':False,'lineage':'SRF v0.3 OrganPrototype field contract; original heuristic scores intentionally not invoked'}


def execute(plugin_id,parameters):
    p=validate(plugin_id,parameters)
    if plugin_id=='structural_plasticity':
        from .morphogenesis import structural_plasticity
        function=structural_plasticity
    elif plugin_id=='kan_benchmark':
        from .kan import execute as function
    elif plugin_id=='cortical_sequence':
        from .cortical import execute as function
    else:
        function={'coupled_dynamics':coupled,'regression_benchmark':regression,'quantum_circuit':quantum,'legacy_topology':topology}[plugin_id]
    result=function(p)
    result['limitations']=next(x['limitations'] for x in SPECS if x['id']==plugin_id)
    result['parameters']=p
    return result
