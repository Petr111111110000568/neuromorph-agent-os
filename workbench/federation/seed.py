"""Explicit, idempotent starter content. No claimed external participation."""

def seed_exchange(exchange):
    title='Пилот: воспроизводимость моделей клеточного состояния'
    existing=next((x for x in exchange.list_offers(public=True)['items'] if x.get('title',x.get('terms',{}).get('title'))==title),None)
    resource_title='Методика приёмки вычислительного исследования Meta-Harness'
    resource=next((x for x in exchange.list_resources(public=False)['items'] if x.get('title')==resource_title),None)
    if resource is None:
        resource=exchange.add_resource({'title':resource_title,'summary':'Авторская памятка проекта: разбиение по донорам, baseline, артефакты и область вывода.',
          'content':'Перед расчётом задайте вопрос, единицу наблюдения, разрешённый набор данных, метрику и простой baseline. Разделите настройку и итоговую проверку; исключите пересечение доноров или связанных наблюдений. Сохраните версии, параметры, seed, SHA-256 и отрицательные результаты. Независимый проверяющий воспроизводит вычисление и отмечает пределы переноса. Сходство выражений генов и успех программы не доказывают причинность или изменение организма.',
          'license':'CC-BY-4.0; Meta-Harness project-authored starter summary','data_class':'public',
          'rights_confirmed':True,'redistribution_allowed':True,'kind':'research_summary','cost_credits':2})
    offer=existing or exchange.create_offer({'title':title,
      'description':'Сопоставить две опубликованные оценки переноса моделей клеточного состояния между независимыми донорами; вернуть источники, разбиения и ограничения.',
      'task_type':'literature_review','requirements':['Две проверяемые первичные ссылки с DOI или URL авторов','Описание единицы наблюдения и независимости test split','Простой baseline и условия применимости; недостаток данных обозначить явно'],
      'reward_credits':3,'max_assignments':2,'data_class':'public','lease_seconds':3600})
    return {'offer':offer,'resource':resource,'note':'Starter examples only. No external agents joined; no private data loaded.'}
