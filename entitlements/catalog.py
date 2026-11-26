"""Units and product scopes only. Prices remain in the existing product modules."""
PRODUCTS = {
    'image_credit': frozenset({'postcard', 'vision_board'}),
    'photo_frame': frozenset({'photosession'}),
    'animation': frozenset({'animation'}),
    'audio_order': frozenset({'song_short', 'song_full'}),
    'musical_order': frozenset({'musical_direct', 'musical_upsell'}),
    'cartoon_order': frozenset({'mother', 'mother_bday', 'cartoon_short', 'cartoon_long'}),
    'ded_moroz_order': frozenset({'ded_single', 'ded_family'}),
}
ORDER_SCOPED = frozenset({'photo_frame', 'audio_order', 'musical_order', 'cartoon_order', 'ded_moroz_order'})
SINGLE = frozenset({'audio_order', 'musical_order', 'cartoon_order', 'ded_moroz_order'})
BUCKETS = frozenset({'paid', 'bonus', 'promo', 'trial', 'compensation'})
SOURCES = frozenset({'payment.prodamus', 'support', 'campaign', 'trial_policy', 'synthetic'})
