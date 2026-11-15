from PIL import Image
import pytest

from ded_moroz_intro import genitive_name, recipient_line, create_intro


@pytest.mark.parametrize('name,form',[
    ('Маша','Маши'),('Машенька','Машеньки'),('Мишенька','Мишеньки'),
    ('Аня','Ани'),('Дима','Димы'),('Саша','Саши'),('Ванечка','Ванечки'),
    ('Любовь','Любови'),('Лев','Льва'),('Павел','Павла'),('Никита','Никиты'),
    ('Анна-Мария','Анны-Марии'),
])
def test_russian_proper_names_and_diminutives(name,form):
    assert genitive_name(name)==form
    assert recipient_line(name)=='для '+form


@pytest.mark.parametrize('name',['Солнышко','Alex','Мой зайчик'])
def test_unknown_names_do_not_get_guessed_endings(name):
    assert genitive_name(name) is None
    assert recipient_line(name)==name+', с Новым годом!'


def test_exact_manual_form_and_intro_are_local_and_preserved(tmp_path):
    target=tmp_path/'intro.png'
    assert create_intro('Солнышко',target,name_genitive='моего солнышка')=='для моего солнышка'
    assert 'для моего солнышка' in target.with_suffix('.txt').read_text()
    with Image.open(target) as image:
        assert image.size==(1920,1080) and image.mode=='RGB'
        # Large central text is visibly distinct from the dark background.
        assert max(image.crop((230,555,1690,770)).getchannel('R').getextrema())>230


@pytest.mark.parametrize('form',['','x'*121,'Маши\nДимы','[warm] Маши'])
def test_invalid_manual_caption_is_rejected(form):
    with pytest.raises(ValueError):recipient_line('Маша',form)
