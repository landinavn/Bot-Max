"""Offline montage: remove idle tails, preserve complete speech, scan the final QR."""
import copy
import json
import math
from uuid import uuid4

import pytest
from PIL import Image
import zxingcpp

from config import DEFAULT_BOT_LINK
from multstudio import repository as repo
from multstudio.assembly_editing import trim_seconds
from multstudio.closing_card import create
from multstudio.episode_three_delivery import apply
from multstudio.episode_three_revision import apply as revise
from multstudio.episode_three_trim import apply as remove_pair
from multstudio.render import Renderer,duration,process
from multstudio.episode_two import speech_profiles
from test_multstudio import studio,add_image
from test_multstudio_episode_three import ready,attach_audio,make_clip
from test_multstudio_anya_broll import old_series
from test_multstudio_av_sync import samples,rms


async def approved_shot(studio,seconds=2.2,provider='heygen'):
    snap=await attach_audio(studio,await ready(studio),seconds=seconds)
    source=await make_clip(studio[2],5,audio_seconds=seconds)
    job=uuid4().hex
    async with repo.connect() as db:
        await db.execute("INSERT INTO cartoon_jobs(id,project_id,target,target_id,kind,snapshot,fingerprint,status,estimate,expires,usage) VALUES (?,?,'scene',?,'video',?,?,'done',0,0,?)",
            (job,snap['project_id'],snap['id'],repo.encoded(dict(spec=snap['spec'],quote={'video_provider':provider})),job,repo.encoded({'provider':provider})))
        await db.commit()
    await repo.add_asset(snap['project_id'],'scene',snap['id'],'video',snap['context_hash'],source.name,
        studio[2].checksum(source),approved=1,job_id=job,
        metadata=repo.encoded({'duration_seconds':5,'image_id':snap['assets']['image']['id'],'speech_id':snap['assets']['speech']['id']}))
    snap=await repo.snapshot('scene',snap['id']);snap['quote']={'speech_profiles':await speech_profiles(snap)}
    snap['assembly_editing']={'video_end_trim':2,'source_video_provider':provider}
    return snap


async def test_shorten_two_seconds_and_keep_full_audio_and_sources(studio):
    shot=await approved_shot(studio);assets=studio[2]
    before={name:assets.checksum(assets.path(shot['assets'][name]['path'])) for name in ('video','speech')}
    output=await Renderer(assets).scene(shot)
    assert abs(await duration(output)-3)<.035
    audio=await samples(output)
    assert rms(audio,.05,.2)>.04 and rms(audio,2.0,2.15)>.04
    assert rms(audio,2.6,2.9)<.001
    assert all(assets.checksum(assets.path(shot['assets'][name]['path']))==digest for name,digest in before.items())


async def test_long_word_ending_is_never_cut_to_force_three_seconds(studio):
    shot=await approved_shot(studio,seconds=4.1)
    expected=math.ceil(await duration(studio[2].path(shot['assets']['speech']['path']))*30-1e-9)/30
    output=await Renderer(studio[2]).scene(shot)
    assert abs(await duration(output)-expected)<.035 and expected>4.0
    assert rms(await samples(output),3.95,4.05)>.04


@pytest.mark.parametrize('provider,expected',[('kling',5),('heygen_video',3),(None,5)])
async def test_tail_is_only_removed_from_verified_heygen_sources(studio,provider,expected):
    shot=await approved_shot(studio,provider=provider)
    output=await Renderer(studio[2]).scene(shot)
    assert abs(await duration(output)-expected)<.035


async def test_migration_preserves_all_scene_assets_and_scopes_provider_from_original_job(studio):
    project,_,_=studio;episode,_,_=await old_series(studio)
    await revise(project,456);await remove_pair(project,456)
    heygen=await approved_shot(studio,provider='heygen')
    before={s['id']:await repo.snapshot('scene',s['id']) for s in await repo.children('scene',episode)}
    prior=[await repo.snapshot('episode',e['id']) for e in await repo.children('episode',project) if e['number']<3]
    await apply(project,456,DEFAULT_BOT_LINK)
    snap=await repo.snapshot('episode',episode)
    modified=next(s for s in snap['scenes'] if s['id']==heygen['id'])
    assert modified['assembly_editing']=={'video_end_trim':2,'source_video_provider':'heygen'}
    assert all('assembly_editing' not in s for s in snap['scenes'] if s['id']!=heygen['id'])
    for key,value in before.items():
        assert await repo.snapshot('scene',key)==value
    assert [await repo.snapshot('episode',e['id']) for e in await repo.children('episode',project) if e['number']<3]==prior
    assert snap['spec']['closing_card']['bot_url']==DEFAULT_BOT_LINK
    before_episode=await repo.get('episode',episode)
    await apply(project,456,DEFAULT_BOT_LINK)
    assert await repo.get('episode',episode)==before_episode
    with pytest.raises(ValueError):await apply(project,999,DEFAULT_BOT_LINK)
    assert await repo.claim() is None


async def test_snapshot_never_guesses_provider_from_current_speaking_scene_settings(studio):
    shot=await approved_shot(studio,provider='kling')
    episode=(await repo.get('scene',shot['id']))['episode_id']
    row=await repo.get('episode',episode)
    await repo.edit('episode',episode,dict(row['spec'],heygen_end_trim_seconds=2))
    snap=await repo.snapshot('episode',episode)
    actual=next(s for s in snap['scenes'] if s['id']==shot['id'])
    assert actual['spec']['video_provider']=='heygen' and 'assembly_editing' not in actual


@pytest.mark.parametrize('amount',[-1,3,True,float('nan'),'2'])
def test_invalid_trim_cannot_disable_duration_safeguards(amount):
    with pytest.raises(ValueError):trim_seconds(amount)


def test_russian_closing_qr_decodes_to_exact_configured_bot_link(tmp_path):
    poster=tmp_path/'closing.png';create(poster,DEFAULT_BOT_LINK)
    with Image.open(poster) as image:
        assert image.size==(1080,1920)
        result=zxingcpp.read_barcode(image)
        assert result and result.text==DEFAULT_BOT_LINK
        assert image.getpixel((100,230))!=(0,0,0)
    with pytest.raises(ValueError):create(poster,'https://other.example/bot')


async def test_final_mp4_has_scannable_russian_card_instead_of_provider_video(studio):
    assets=studio[2];assets.root.mkdir(parents=True,exist_ok=True)
    background=assets.path('apartment.png');Image.new('RGB',(1080,1920),'#78634b').save(background)
    # The final provider file is deliberately unreadable: its English/black end slate must never be used.
    bad=assets.path('english-black.mp4');bad.write_bytes(b'not used')
    scene={'spec':{'dialogue':[]},'assets':{'image':{'path':background.name,'approved':1},'video':{'path':bad.name}}}
    options={'style':'anya-russian-qr-v1','bot_url':DEFAULT_BOT_LINK,'seconds':5}
    renderer=Renderer(assets)
    output=await renderer.assemble(dict(spec={'closing_card':options},scenes=[scene],assets={}))
    assert abs(await duration(output)-5)<.035
    frame=assets.path('decoded.png')
    await process('ffmpeg','-v','error','-y','-ss','4','-i',output,'-frames:v','1',frame)
    with Image.open(frame) as image:
        decoded=zxingcpp.read_barcode(image)
        assert decoded and decoded.text==DEFAULT_BOT_LINK


async def test_shortened_timeline_shifts_subtitles_and_preserves_total_length(studio):
    shot=await approved_shot(studio)
    second=copy.deepcopy(shot);second['number']=4
    # End card suppresses tail trimming for the last block; use a separate silent final card.
    last={'spec':{'dialogue':[]},'assets':{}}
    options={'style':'anya-russian-qr-v1','bot_url':DEFAULT_BOT_LINK,'seconds':5}
    snapshot=dict(spec={'ending':['Продолжение следует…'],'closing_card':options},scenes=[shot,second,last],assets={})
    output=await Renderer(studio[2]).assemble(snapshot)
    assert abs(await duration(output)-11)<.035
    srts=[p.read_text() for p in studio[2].root.glob('*.srt')]
    assert any('00:00:03,000 -->' in text and '\n2\n' in text for text in srts)


async def test_new_quote_freezes_montage_settings_and_cancels_old_assembly_without_generation(studio):
    from multstudio import rules
    project,settings,assets=studio;episode,_,_=await old_series(studio)
    await revise(project,456);await remove_pair(project,456)
    for row in await repo.children('scene',episode):
        snap=await repo.snapshot('scene',row['id']);await add_image(assets,snap)
        snap=await repo.snapshot('scene',row['id'])
        if snap['spec'].get('dialogue'):
            snap['quote']={'speech_profiles':await speech_profiles(snap)}
            await repo.add_asset(project,'scene',row['id'],'speech',snap['context_hash'],'retained.mp3','sha',approved=1,
                metadata=repo.encoded(rules.speech_metadata(snap,2)))
        snap=await repo.snapshot('scene',row['id'])
        await repo.add_asset(project,'scene',row['id'],'video',snap['context_hash'],'retained.mp4','sha',approved=1,
            metadata=repo.encoded({'duration_seconds':5,'image_id':snap['assets']['image']['id'],'speech_id':snap['assets'].get('speech',{}).get('id')}))
    old=await repo.quote('episode',episode,'assemble',settings)
    assert json.loads(old['snapshot'])['quote']['render_version']==4
    await apply(project,456,DEFAULT_BOT_LINK)
    assert (await repo.job(old['id']))['status']=='cancelled'
    fresh=await repo.quote('episode',episode,'assemble',settings)
    frozen=json.loads(fresh['snapshot'])
    assert fresh['id']!=old['id'] and frozen['quote']['render_version']==5
    assert frozen['spec']['heygen_end_trim_seconds']==2 and frozen['spec']['closing_card']['bot_url']==DEFAULT_BOT_LINK
    assert await repo.claim() is None  # Creating the free quote still requires explicit confirmation.
