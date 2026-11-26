"""Owner-selected fixed editing rule, scoped to the two minute-long Mother cartoons."""
FIXED_INTRO_SECONDS = 13.0
FIXED_INTRO_METHOD = 'mother-fixed-intro-v1'
THEMES = frozenset(('theme_mother', 'theme_mother_bday'))


def fixed_intro_for(data):
    return FIXED_INTRO_SECONDS if data.get('cartoon_theme') in THEMES else None


def recoverable_fixed_intro(data):
    """Only a known pre-scene song-alignment failure can be recovered automatically."""
    from mother_recovery import urls
    from song_alignment import alignment_source
    if fixed_intro_for(data) is None:
        return False
    story = data.get('cartoon_kling_story') or {}
    if (story.get('shot_seconds', 10) != 10 or len(story.get('shots', [])) != 6
            or story.get('result_url') or data.get('video_merged_url')):
        return False
    if any(any(shot.get(key) for key in ('image_submitting', 'image_url', 'video_submitting',
               'task_id', 'video_url', 'end_frame_url')) for shot in story['shots']):
        return False
    image, audio = urls(data)
    lyrics = data.get('song_review_lyrics') or data.get('video_song_lyrics') or ''
    if not image or not audio or not lyrics:
        return False
    source = alignment_source(audio, lyrics)
    error = story.get('alignment_error') or {}
    attempts = story.get('alignment_attempts', 0)
    exhausted = type(attempts) is int and attempts >= 2
    return (story.get('alignment_submitting') == source
            or error.get('source') == source
            or story.get('alignment_attempt_source') == source and exhausted)
