"""An already paid campaign order must finish even after the promotion ends."""
import pytest
import black_friday as friday
from musical_orders import PaidMusicalWorker
from test_musical_upfront import test_paid_flow_persists_inputs_and_delivers_mp4_without_audio_preview as _paid_flow
from test_sales_stats import sales_payments
from test_handlers import (event_replies,max_image_upload,isolated_example_directory,text_correction,
                           accepted_song_draft_for_legacy_regressions)


async def test_paid_campaign_video_finishes_after_expiry(credit_db,sales_payments,monkeypatch):
    monkeypatch.setattr(friday,'now',lambda:friday.START+60)
    process=PaidMusicalWorker.process
    async def expired_process(worker,key):
        monkeypatch.setattr(friday,'now',lambda:friday.END+1)
        return await process(worker,key)
    monkeypatch.setattr(PaidMusicalWorker,'process',expired_process)
    await _paid_flow(credit_db,sales_payments,monkeypatch,False)
