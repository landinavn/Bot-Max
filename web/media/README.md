# Private media provenance — Frontend 1.2

Owner explicitly approved publication of the nine named files on 26 November 2026, including identifiable adults and children. The additional two postcard banners and `ded_moroz_kids.mp4` were supplied in this same design task for public use. This is the owner's authorization; it is not an independently verified music or likeness licence.

Only the allowlisted source files were processed. `media/manifest.json` (outside public) records the exact source, SHA-256, public output, byte count, transformation and authorization. Metadata is removed by re-encoding; private URLs, order IDs, DB records and credentials are never copied. Audio/video are opt-in, without autoplay, with `preload="none"`.

| Source | Actual content / use |
| --- | --- |
| before.jpg + after.jpg | Woman on embankment → personalized vision collage. Only verified matching standalone pair; interactive comparison. |
| photo_fam_1.webp | New Year family photo collage; hero/catalog/season. |
| photo_fam_2.webp, photo_fam_3.webp | Additional family photosession collages; gallery. PNG names in brief are absent; these WEBP counterparts exist. |
| photo_main_1.webp | Byte-identical to photo_fam_1.webp; deduplicated, not copied again. |
| cartoon_example.mp4 | Birthday cartoon with girl; video + poster, public use explicitly approved. |
| musical-card.mp4 | Christmas/New Year musical postcard; video + poster. Static frame is explicitly labeled as such. |
| den_mama.mp4 | One-minute film about mother; video + poster + same soundtrack as an audio excerpt (not a full-length song sample). Found in owner's earlier upload, not in examples. |
| ded_moroz_kids.mp4 | Owner's new upload; family greeting; poster at 8s shows actual Santa. |
| c60aa946-daf8-4616-8945-19c0136ef7d8.png | Birthday postcard promotional before/after banner, kept intact. |
| 035c843a-5992-42b4-8c4b-8dfe63cd409c.png | Mother's Day postcard promotional before/after banner, kept intact. |

Banners contain an embedded Telegram symbol. That is part of the original artwork, not an active link. All site creation buttons use the verified MAX URL. Banner halves are not invented as separate source files.

Missing: `animated_photo.mp4`, `cartoon_example1.mp4`, `mark_ded_moroz.mp4`, `NY_card.png`. No matching before/after pairs for photosessions/cartoons/animation have been supplied; their comparison tabs show actual finished work or an explicit unavailable state. No random image is substituted for the input.

Original square logo received in 412b5728-b2e3-4df7-b50d-3e2e1a52a2be.zip; PNG 1254×1254. Resized without cropping or stretching to 512px WebP, ICO (16–64), 32px PNG and 180px Apple icon. Used alongside the official title, not as a horizontal replacement.

Other files in owner examples (including rabbit, mother/daughter and unlisted postcard images) have not been copied: explicit publication approval for those file names has not been recorded.

## Reproduce

Requires Pillow and ffmpeg, no network/generative API:

```sh
python scripts/prepare-media.py --examples /path/to/examples --mother-video /path/to/den_mama.mp4 --santa-video /path/to/ded_moroz_kids.mp4 --banners /path/to/unpacked-approved-banners --logo /path/to/original-logo.png
```

Unused SVG demo assets from Frontend 1.0 have been removed from public and the static release. The existing text-only OG cover is retained; it makes no claim to be a customer result. Do not run the legacy demo generation script to fill missing media.


Frontend 1.2: two duplicate postcard records and the accidental logo README entry were removed. Every public media file has one provenance record and an output SHA-256. The public folder contains no README or registry. Both advertising banners still include the original Telegram mark: owner-approved replacement artwork is required before public launch; the originals were not retouched. The preview requires server-side Basic Auth, not merely noindex.
