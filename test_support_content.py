import asyncio
import pytest

from question_auto import answer_text
from support_content import (clarification, content_issues, direct_answer,
                             formal_address, size_guide)

GYM = {'id': 'MLA2055004735', 'title': 'Guantes Gym Negro M', 'attributes': []}
NF = {'id': 'MLA1790499567', 'title': 'Guantes Gym Pesas Muñequera Northfitness M', 'attributes': []}
CODERA = {'id': 'MLA3196261518', 'title': 'Codera',
          'attributes': [{'id': 'IS_ADJUSTABLE', 'value_name': 'Sí'}]}


def test_distinct_size_evidence_and_no_title_guessing():
    assert size_guide(GYM)['sizes_cm']['M'] == [9, 15]
    assert size_guide(NF)['sizes_cm']['M'] == [19, 20.5]
    assert size_guide({'title': NF['title'], 'id': 'MLA999'}) is None
    assert size_guide(dict(NF, title='Kit Guantes + Straps')) is None


def test_recognized_new_variant_sku():
    item = {'id': 'MLA999', 'title': 'Guantes NF Rosa L',
            'attributes': [{'id': 'SELLER_SKU', 'value_name': 'GNF01RSL'}]}
    assert size_guide(item)['label'] == 'Guantes NF'
    item['attributes'][0]['value_name'] = 'GNF01RSL-OTHER'
    assert size_guide(item) is None


@pytest.mark.parametrize('question', ['Estoy entre S y M, qué talle elijo?',
    'Medí 19 cm de circunferencia, sería S?', 'El ancho incluye el pulgar?'])
def test_gym_follows_owner_selected_photo_without_circumference_conversion(question):
    text = direct_answer(question, GYM)
    assert '?' in text
    assert 'te recomiendo' not in text.lower()
    assert 'incluyendo el pulgar' in text
    assert 'No es circunferencia' in text
    assert 'no permite confirmar' not in text
    assert not content_issues(text, question)


def test_nf_table_method_and_stock_are_separate():
    text = direct_answer('Cómo mido para elegir talle?', NF)
    assert 'circunferencia' in text and 'sin incluir el pulgar' in text
    assert '19–20,5' in text and '22–23,5' in text
    assert 'no confirma disponibilidad' in text


def test_medical_query_does_not_substitute_condition_or_promise_relief():
    text = direct_answer('Tienen para epitrocleitis?', CODERA)
    assert 'epicondilitis' not in text
    assert 'No podemos confirmar' in text
    assert 'ajuste regulable' in text
    assert '?' in text and not content_issues(text, 'Tienen para epitrocleitis?')


def test_medical_complaint_stays_in_existing_post_sale_path():
    assert direct_answer('Mi compra vino rota y me causa dolor', CODERA) is None
    assert 'detalle del pedido' in clarification('Mi compra vino rota y me causa dolor')


def test_purchasing_question_not_misclassified_as_existing_complaint():
    assert '¿Qué productos' in clarification('Quiero comprar guantes y straps juntos')


def test_missing_compatibility_asks_one_specific_question():
    text = clarification('Sirve para la barra fija de Cross fit')
    assert 'ejercicio concreto' in text and text.count('?') == 1
    assert 'compatible' not in text


@pytest.mark.parametrize('question', ['Buenos días, cómo mido la mano?', 'Hola! Talle M?'])
def test_politeness_alone_does_not_trigger_usted(question):
    assert not formal_address(question)


def test_usted_is_preserved_when_explicit():
    q = 'Podría usted indicarme las medidas de cada talle?'
    assert formal_address(q)
    text = direct_answer(q, GYM)
    assert '¿Qué valores de A y B obtuvo' in text
    assert not content_issues(text, q)


@pytest.mark.parametrize('text', ['Si tienes dudas, consulta la plataforma de compra.',
    'Puedes medir tu mano.', 'Para ti recomendamos M.', 'Usted puede consultar.'])
def test_previous_wrong_register_is_rejected(text):
    assert content_issues(text, 'Cómo lo mido?')


class Client:
    def __init__(self):
        self.paths = []

    async def get(self, path, *args):
        self.paths.append(path)
        assert path.endswith('/description')  # No recycling unreviewed answers.
        return {'plain_text': 'Cierre regulable con velcro. Incluye un par.'}


class Worker:
    seller = '237699011'

    def __init__(self, answers):
        self.answers = iter(answers)
        self.calls = []

    def public_question_context(self):
        return 'NorthFitness'

    async def draft(self, evidence, **kwargs):
        self.calls.append(dict(evidence))
        return next(self.answers)


def test_bad_draft_regenerates_with_sources_without_sending():
    worker = Worker(['Si tienes dudas consulta la sección de ayuda.',
                     '¡Hola! Tiene cierre regulable con velcro e incluye un par. Saludos, NorthFitness.'])
    client = Client()
    q = {'text': 'Cómo se ajusta?', 'item_id': 'MLA3196261518'}
    text = asyncio.run(answer_text(worker, client, q, CODERA))
    assert 'velcro' in text and len(worker.calls) == 2
    assert worker.calls[0]['description'].startswith('Cierre')
    assert 'past_answers_sample' not in worker.calls[0]
    assert worker.calls[1]['required_corrections']


def test_two_invalid_drafts_fall_back_to_useful_clarification():
    worker = Worker(['Puedes usarlo.', 'Tu producto funciona.'])
    q = {'text': 'Sirve para barra fija de Crossfit?', 'item_id': 'MLA2055005533'}
    text = asyncio.run(answer_text(worker, Client(), q, {}))
    assert 'ejercicio concreto' in text and len(worker.calls) == 2
    assert not content_issues(text, q['text'])


def test_verified_table_does_not_depend_on_model_or_past_answer():
    worker = Worker([])
    q = {'text': 'Medidas de cada talle?', 'item_id': GYM['id']}
    client = Client()
    text = asyncio.run(answer_text(worker, client, q, GYM))
    assert '8,5 × 14' in text and not worker.calls
    assert client.paths == ['/items/' + GYM['id'] + '/description']
