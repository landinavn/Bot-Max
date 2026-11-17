"""Playback order is separate from stable scene IDs/numbers and their media hashes."""


def ordered(scenes, spec):
    timeline=spec.get('scene_order')
    if timeline is None:
        return sorted(scenes,key=lambda s:s['number'])
    if (not isinstance(timeline,list) or any(not isinstance(k,str) for k in timeline)
            or len(set(timeline))!=len(timeline)):
        raise ValueError('Порядок сцен должен содержать уникальные ID.')
    ids={s['id'] for s in scenes}
    if not set(timeline)<=ids:
        raise ValueError('В монтажном порядке есть отсутствующая сцена. Проверьте scene_order серии.')
    positions={key:i for i,key in enumerate(timeline)}
    return sorted(scenes,key=lambda s:(positions.get(s['id'],len(timeline)),s['number']))


def label(scene):
    return str(scene['spec'].get('display_number') or scene['number'])
