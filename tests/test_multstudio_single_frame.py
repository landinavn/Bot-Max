from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from multstudio import repository as repo
from multstudio.adapters import Providers
from multstudio.prompts import KEYFRAMES, scene_image_prompt
from multstudio.seed import SCENES, WORLD
from test_multstudio import studio, ready_scene


def snapshot(number):
    title, duration, names, visual, dialogue, camera, sound = SCENES[number - 1]
    return dict(spec=dict(title=title, duration=duration, visual=visual,
                         camera=camera, sound=sound, dialogue=dialogue),
                world=WORLD, cast=[dict(spec=dict(description=name)) for name in names])


@pytest.mark.parametrize('number', range(1, 8))
def test_every_legacy_scene_selects_one_static_moment_without_script_leak(number):
    snap = snapshot(number)
    before = deepcopy(snap)
    prompt = scene_image_prompt(snap)
    moment, camera = KEYFRAMES[number - 1]
    assert moment in prompt and camera in prompt
    assert snap['spec']['camera'] not in prompt
    assert snap['spec']['sound'] not in prompt
    for _, text, _ in snap['spec']['dialogue']:
        assert text not in prompt
    assert f"Show exactly {len(snap['cast'])} characters" in prompt
    assert 'each appearing exactly once' in prompt
    assert 'Never create a collage, storyboard, diptych' in prompt
    assert 'one camera position' in prompt
    assert snap == before  # Animation direction and approved script remain intact.


def test_scene_two_has_one_shared_view_and_no_separate_closeup():
    prompt = scene_image_prompt(snapshot(2))
    assert 'Дима удобно сидит на диване с телефоном' in prompt
    assert 'Аня рядом с диваном' in prompt
    assert 'Один общий план гостиной с Аней и Димой вместе' in prompt
    assert 'затем крупный план' not in prompt


def test_custom_or_edited_scene_is_not_overwritten_by_seed_direction():
    snap = snapshot(2)
    snap['spec']['visual'] = 'Дима и Аня стоят на балконе.'
    snap['spec']['camera'] = 'FIRST_SHOT then SECOND_SHOT'
    prompt = scene_image_prompt(snap)
    assert 'Дима и Аня стоят на балконе.' in prompt
    assert 'диване' not in prompt
    assert 'FIRST_SHOT' not in prompt and 'SECOND_SHOT' not in prompt
    assert 'Choose only ONE frozen moment' in prompt


async def test_provider_uses_single_frame_prompt_and_keeps_all_edit_references(studio, monkeypatch):
    _, settings, assets = studio
    _, episode = await ready_scene(studio)
    scene = (await repo.children('scene', episode['id']))[1]
    snap = await repo.snapshot('scene', scene['id'])
    snap['quote'] = dict(model='image-test', image_quality='high')
    # Supply two canonical character refs plus a location, as in scene two.
    snap['location_reference'] = dict(id='apartment', approved=1)
    assets.asset_url = AsyncMock(side_effect=['https://cdn.example/anya.png',
        'https://cdn.example/dima.png', 'https://cdn.example/apartment.png'])
    assets.retain = AsyncMock()
    generate = AsyncMock(return_value=[b'image'])
    monkeypatch.setattr('multstudio.adapters._generate_image_bytes', generate)
    from config import Config
    config = Config.from_env()
    provider = Providers(settings, config, assets)
    assert await provider.image(snap) == b'image'
    kwargs = generate.await_args.kwargs
    assert kwargs['prompt_override'] == scene_image_prompt(snap, 'https://cdn.example/apartment.png')
    assert kwargs['style_reference_urls'] == ('https://cdn.example/dima.png',
                                             'https://cdn.example/apartment.png')
    assert generate.await_args.args[0] == 'https://cdn.example/anya.png'
    assert kwargs['count'] == 1
    assets.retain.assert_awaited_once()
    assert assets.retain.await_args.kwargs['data'] == kwargs['prompt_override'].encode()
