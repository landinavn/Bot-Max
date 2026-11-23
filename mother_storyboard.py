"""Versioned minute-long story. These exact strings form image and HeyGen prompts."""
POLICY='mother-one-character-heygen-six-ten-second-shots-v2'
SHOT_SECONDS=10
DURATION=60
BASE=(
    'A coherent, gentle, high-quality stylized 3D animated family film, horizontal 16:9. '
    'Exactly ONE visible character: the same adult mother from the identity reference. '
    'Preserve her recognizable face, real age, grey or natural hair, cream cardigan, pearl earrings, '
    'necklace, body proportions and outfit throughout. Do not beautify or make her younger. '
    'Use the reference portrait for identity ONLY, never copy its close-up composition. '
    'Wide or medium-wide 35mm eye-level camera, full body or knees-up with surrounding furniture. '
    'Her figure occupies at most 60% of frame height; her face is a small part of the frame. '
    'Keep her entire head, hands, props and relevant furniture inside the central widescreen safe area '
    'with comfortable margins. No headshot, face close-up, hand close-up, zoom-in, orbit or abrupt camera moves. '
    'Same apartment in every shot: sunset window and wooden sideboard at left, oval wooden dining '
    'table at center, cream armchair and warm floor lamp at right. Warm natural light stays constant. '
    'Dining table has exactly three cream plates, three matching cups and cutlery, fixed in place. '
    'The flower vase belongs on the SIDEBOARD, never on the dining table. '
    'Use the establishing frame for room layout and appearance; the latest continuity frame records '
    'the completed actions. Follow the specified starting state of THIS shot. '
    'Objects keep their positions after being placed; do not reset, remove, duplicate or teleport them. '
    'No other people, reflected faces, visible photo portraits, crowds, animals, readable text, subtitles, '
    'logos, watermarks, birthday decorations or Christmas ornaments. One uninterrupted composition. '
    'Mother is a SILENT character: lips stay gently CLOSED for the entire shot, including her smile. '
    'No speech, singing, humming, lip-sync or mouth articulation. No dialogue or voice-over; '
    'no generated music. A separate off-screen singer will be added in post-production. '
)
MOTION_RULE=(
    'Duration exactly 10 seconds, one continuous take. 0–2 seconds: establish the starting pose. '
    '2–7 seconds: perform the specified action ONCE, slowly and naturally. 7–10 seconds: settle '
    'into the completed pose with subtle breathing and a gentle closed-lip smile. '
    'Do not restart, reverse, loop or repeat the action. No extra hand gestures or business, '
    'no freeze frame, speed ramp, time lapse, internal cuts or repeated manipulation. '
    'Camera remains locked or drifts laterally by less than 10 cm; never moves closer. '
)
SHOTS=(
    {'label':'Цветы — один бережный жест',
     'start':'Wide establishing view of the apartment. Mother stands beside the sideboard, holding '
             'one bouquet above an empty stable vase. The dining table already has three plates, '
             'three cups and cutlery; one folded napkin and the pie are not yet on it. '
             'A plain closed album lies beside the armchair. Show the room and her full figure.',
     'action':'She lowers the bouquet into the sideboard vase ONCE and lets go. The stems stay '
              'inside the vase; she does not take them out, rearrange or insert them again. '
              'She rests her hands and looks at the flowers with a small closed-lip smile. '
              'End state: flowers remain in the vase for ALL later scenes.'},
    {'label':'Последний штрих сервировки',
     'start':'Medium-wide knees-up side view of the same dining table. Flowers are already in '
             'the vase on the sideboard. All three plates, cups and cutlery are already in place. '
             'Mother holds one folded cream napkin above the nearest plate. No pie yet.',
     'action':'She places the folded napkin beside the nearest plate ONCE, then lowers her hands. '
              'She does not touch, stack, remove or rearrange plates and cups. '
              'End state: three complete place settings remain on the table.'},
    {'label':'Домашний пирог к чаю',
     'start':'Wide three-quarter view. The sideboard bouquet and three completed place settings '
             'remain exactly as before. Mother stands next to the table holding a round homemade '
             'apple pie on one plain serving board just above the clear center of the table. '
             'The pie is whole, with no candles, lettering or birthday decorations.',
     'action':'She sets the serving board and pie in the center ONCE, releases the board and rests '
              'her hands. Do not lift or move it again and do not serve slices. '
              'End state: whole pie in the center, all three place settings intact.'},
    {'label':'Приглашение к готовому столу',
     'start':'Wide view showing mother from head to knees beside the FULLY SET table. '
             'The same whole pie, three plates, cups, cutlery and folded napkin are clearly visible. '
             'The bouquet stays on the sideboard. The doorway is visible but contains no people.',
     'action':'She looks toward the doorway and makes ONE small open-palm invitation toward '
              'the prepared table. Then her arm rests naturally. Her lips stay closed; she says '
              'nothing. No guests enter. Do not clear the table. '
              'End state: table stays fully set, with pie and cups in their established positions.'},
    {'label':'Альбом в любимом кресле',
     'start':'Wide seated view of mother ALREADY sitting comfortably in the cream armchair by '
             'the floor lamp, feet and armrests visible. She holds the closed plain album on her lap, '
             'NOT at the dining table. In the background the prepared table, whole pie and bouquet '
             'retain their positions. No walking or sitting-down transition inside this shot.',
     'action':'She slowly opens the album ONCE on her lap and looks at the first open spread. '
              'Pages contain only soft abstract memory shapes, no readable lettering or faces. '
              'A small cream keepsake card with a simple red paper heart is tucked on the right '
              'page. Do not turn pages repeatedly. End state: album open on her lap, card visible.'},
    {'label':'Сохранённая открытка — тёплый финал',
     'start':'Medium-wide three-quarter armchair view, full seated figure and room still visible. '
             'Mother remains in the SAME chair. The same album is open on her lap and the small '
             'cream card with a red paper heart rests on its right page. The table behind her '
             'still has all three place settings and the whole pie. Bouquet stays on sideboard.',
     'action':'She lifts the keepsake card ONCE and gently holds it against her chest, then looks '
              'up with warm eyes and a tender closed-lip smile. Album remains open and supported '
              'on her lap. No speaking, kissing, waving, page turning or second gesture. '
              'End on her quiet natural smile and the warm room, without a black screen or text.'},
)


def image_prompt(index):
    shot=SHOTS[index]
    return BASE+'\nFIRST FRAME ONLY: '+shot['start']+'\nShow the moment BEFORE the action starts. Do not depict a later result or a collage.'


def video_prompt(index):
    shot=SHOTS[index]
    return BASE+'\nSTARTING STATE: '+shot['start']+'\nACTION AND END STATE: '+shot['action']+'\n'+MOTION_RULE
