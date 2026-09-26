"""Reviewed resource catalogue and bounded, non-executing research protocols."""
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit
from .studio import ID, invalid
from .store import canonical

CATALOG = Path(__file__).resolve().parent.parent / 'config' / 'science_catalog.json'
TEMPLATES = [
    {'id':'literature-review', 'name':'Обзор доказательств',
     'description':'Сопоставить утверждения с первичными источниками и контрпримерами.',
     'steps':['Зафиксировать вопрос и критерии включения', 'Найти первичные источники',
              'Выделить подтверждения и противоречия', 'Отделить вывод от предположения',
              'Сохранить ограничения и независимую рецензию']},
    {'id':'reproduce-method', 'name':'Воспроизведение метода',
     'description':'Подготовить ограниченный эксперимент по опубликованному методу.',
     'steps':['Закрепить статью, код, данные и лицензии', 'Описать контроль и метрику',
              'Задать версии, seed, параметры и бюджет', 'Проверить код до исполнения',
              'Сохранить фактический запуск, ошибки и сравнение с публикацией']},
    {'id':'compare-approaches', 'name':'Сравнение подходов',
     'description':'Сравнить методы на одинаковых данных и заранее выбранной метрике.',
     'steps':['Определить общий набор данных и baseline', 'Задать равные условия и бюджет',
              'Разделить разработку и оценку', 'Проверить контрпример и устойчивость',
              'Сохранить результаты каждого запуска и неопределённость']},
]


def safe_url(value):
    if not isinstance(value,str) or len(value)>2048 or value!=value.strip():
        return False
    try:
        p = urlsplit(value)
        return (p.scheme == 'https' and bool(p.hostname) and not p.username and not p.password
                and p.port in (None,443) and not p.fragment)
    except (ValueError, TypeError):
        return False


def catalogue(path=None):
    """Read only a shipped catalogue; no remote fetching or package installation."""
    try:
        raw = (Path(path) if path is not None else CATALOG).read_bytes()
        if len(raw) > 1_000_000:
            raise ValueError('size')
        data = json.loads(raw)
        if not isinstance(data,dict):
            raise ValueError('root')
        items = data['items']
        if data['schema_version'] != 1 or not isinstance(items,list) or not 1 <= len(items) <= 100:
            raise ValueError('schema')
        seen = set()
        for item in items:
            if not isinstance(item,dict) or not ID.fullmatch(item['id']) or item['id'] in seen:
                raise ValueError('id')
            seen.add(item['id'])
            for field in ('name','category','summary','status','license','access','integration','next_action','limitations','reviewed_at'):
                if not isinstance(item[field],str) or not item[field] or len(item[field]) > 8000:
                    raise ValueError('field')
            if not safe_url(item['url']) or not isinstance(item['primary_sources'],list) or len(item['primary_sources']) > 12:
                raise ValueError('url')
            if any(not safe_url(url) for url in item['primary_sources']):
                raise ValueError('source')
        return {**data, 'templates':TEMPLATES,
                'catalog_digest':hashlib.sha256(canonical(data).encode()).hexdigest(),
                'notice':'Каталог отражает проверку источников, а не подключение аккаунтов или запуск моделей.'}
    except (OSError,ValueError,KeyError,TypeError,UnicodeError,RecursionError) as exc:
        raise invalid('Science catalogue is unavailable', 'catalog_unavailable',503) from exc


def protocol_task(data, catalog=None):
    """Compile one proposed study; nothing here executes its method or sends data."""
    fields = {'project_id','idempotency_key','template_id','platform_id','title','question',
              'hypothesis','method','control','criteria'}
    if not isinstance(data,dict) or set(data) != fields:
        raise invalid('Expected complete research protocol fields')
    clean = {}
    for field in fields:
        value = data[field]
        limit = 200 if field == 'title' else 4000 if field in ('question','criteria') else 2000
        if not isinstance(value,str) or not value.strip() or len(value)>limit or '\x00' in value:
            raise invalid('Invalid protocol field: '+field)
        clean[field] = value.strip()
    for field in ('project_id','idempotency_key','template_id','platform_id'):
        if not ID.fullmatch(clean[field]):
            raise invalid('Invalid protocol identifier')
    cat = catalog if catalog is not None else catalogue()
    template = next((t for t in TEMPLATES if t['id']==clean['template_id']),None)
    platform = next((p for p in cat['items'] if p['id']==clean['platform_id']),None)
    if template is None or platform is None:
        raise invalid('Unknown research template or platform')
    # No timestamp/live availability in the payload: retrying the same input stays idempotent.
    body = '\n\n'.join([
        'ПРОТОКОЛ ИССЛЕДОВАНИЯ — план, исполнение не запущено.',
        'Версия протокола: 1\nСценарий (ID): '+clean['template_id'],
        'Предлагаемый инструмент (ID каталога): '+clean['platform_id'],
        'Гипотеза: '+clean['hypothesis'], 'Метод: '+clean['method'],
        'Контроль / контрпример: '+clean['control'],
        'Этапы протокола v1: источники и лицензии → контроль → проверка метода → '
        'ограниченный запуск после допуска → результаты и рецензия. '
        'Название, адрес и доступность инструмента проверяются в каталоге перед отдельным запуском.',
        'Квитанция будущего запуска: commit, лицензия, хеш входных данных, параметры, seed, среда, '
        'фактическая модель, затраты, stdout/stderr, выходные файлы и способ проверки. '
        'До запуска все эти результаты отсутствуют.',
        'Дополнительные расходы: 0. Доступ и тариф проверяются отдельно. '
        'Модельный ответ и согласие агентов не являются научным подтверждением.'
    ])
    if len(body)>12000:
        raise invalid('Compiled research protocol is too large')
    return {'kind':'task','project_id':clean['project_id'],'idempotency_key':clean['idempotency_key'],
            'title':clean['title'],'question':clean['question'],'criteria':clean['criteria'],
            'body':body,'status':'planned'}
