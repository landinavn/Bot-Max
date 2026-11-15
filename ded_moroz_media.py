"""Normalize a MAX child photo without weakening pixel/byte/format validation."""
import io
import warnings

from PIL import Image,ImageOps,UnidentifiedImageError


def child_png(path):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error',Image.DecompressionBombWarning)
            with Image.open(path) as image:
                if image.format not in ('JPEG','PNG','WEBP'):
                    raise ValueError('Отправьте фотографию JPG/PNG. Фото, преобразованное MAX в WEBP, тоже подходит.')
                if image.width*image.height>40_000_000 or min(image.size)<1:
                    raise ValueError('Фотография должна содержать не более 40 000 000 пикселей.')
                if getattr(image,'is_animated',False):raise ValueError('Отправьте обычную фотографию без анимации.')
                image.load()
                output=io.BytesIO();ImageOps.exif_transpose(image).convert('RGB').save(output,format='PNG')
                return output.getvalue()
    except (Image.DecompressionBombWarning,Image.DecompressionBombError):
        raise ValueError('Фотография должна содержать не более 40 000 000 пикселей.') from None
    except (UnidentifiedImageError,OSError):
        raise ValueError('Не удалось прочитать фотографию. Отправьте JPG/PNG.') from None
