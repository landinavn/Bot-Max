"""Still keyframes rest with closed lips; animation/dialogue inputs are unchanged."""
import base64
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from config import Config
from multstudio.adapters import Providers
from multstudio.episode_three import SCENES
from multstudio.prompts import CLOSED_MOUTH_KEYFRAME, scene_image_prompt
from multstudio.rules import normalize
from multstudio.settings import Settings
from test_multstudio_single_frame import snapshot


@pytest.mark.parametrize('number',range(1,8))
def test_closed_lips_for_every_existing_scene_without_rewriting_snapshot(number):
    snap=snapshot(number);before=deepcopy(snap)
    prompt=scene_image_prompt(snap)
    assert prompt.endswith(CLOSED_MOUTH_KEYFRAME)
    assert 'No visible teeth, tongue or open mouth' in prompt
    assert 'eyes, eyebrows, gaze and body pose' in prompt
    assert snap==before


@pytest.mark.parametrize('title,speaker,text,visual',SCENES)
def test_every_third_episode_scene_uses_closed_mouth_resting_pose(title,speaker,text,visual):
    spec=normalize(dict(title=title,visual=visual,characters=[speaker] if speaker else [],
        dialogue=[dict(character_id=speaker,text=text)] if speaker else []))
    snap=dict(spec=spec,world={},cast=[dict(spec={'description':speaker})] if speaker else [],
              location_reference={'approved':1})
    prompt=scene_image_prompt(snap,'https://example.org/apartment.png')
    assert prompt.endswith(CLOSED_MOUTH_KEYFRAME)
    assert 'https://example.org/apartment.png' in prompt
    assert ('Only this mouth' not in prompt and 'Animate only this mouth' not in prompt)
    if speaker:
        assert 'Everyone else stays off screen' in prompt
    else:
        assert 'Show exactly 0 characters' in prompt


def test_open_mouth_visual_or_reference_does_not_override_closed_lips():
    snap=dict(spec={'visual':'Героиня широко открыла рот, видны зубы.', 'title':'Мой кадр'},
              world={'expression':'open mouth'},cast=[{'spec':{'description':'Улыбка с зубами'}}])
    prompt=scene_image_prompt(snap)
    assert prompt.rfind('mouth fully closed')>prompt.index('Улыбка с зубами')
    assert 'overrides any open-mouth expression' in prompt
    assert 'change only the mouth pose' in prompt


@pytest.mark.parametrize('with_reference',[False,True])
async def test_character_reference_creation_and_edit_keep_the_same_rule(tmp_path,monkeypatch,with_reference):
    assets=SimpleNamespace(asset_url=AsyncMock(return_value='https://example.org/ref.png'),retain=AsyncMock())
    provider=Providers(Settings(root=tmp_path,image_model='image-test'),Config.from_env(),assets)
    snap=dict(target='character',world={},spec={'description':'Улыбка с открытым ртом'},
              assets={'image':{'id':'approved-reference'}} if with_reference else {},
              quote={'model':'image-test','image_quality':'high'})
    if with_reference:
        generate=AsyncMock(return_value=[b'png'])
        monkeypatch.setattr('multstudio.adapters._generate_image_bytes',generate)
        assert await provider.image(snap)==b'png'
        prompt=generate.await_args.kwargs['prompt_override']
    else:
        generate=AsyncMock(return_value=SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(b'png').decode())]))
        @asynccontextmanager
        async def client(config):
            yield SimpleNamespace(images=SimpleNamespace(generate=generate))
        monkeypatch.setattr('multstudio.adapters._image_api_client',client)
        assert await provider.image(snap)==b'png'
        prompt=generate.await_args.kwargs['prompt']
    assert prompt.endswith(CLOSED_MOUTH_KEYFRAME)
    assert assets.retain.await_args.kwargs['data']==prompt.encode()
