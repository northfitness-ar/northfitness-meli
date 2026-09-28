"""Reviewed public product evidence and deterministic support content checks.

Sources visually reviewed 2026-09-28 in NORTHFITNESS-OFICIAL / Imágenes Productos /
Productos. On 2026-09-28 the owner instructed support to follow the Gym photo
literally (A/B), without reinterpreting it as glove dimensions. Tables do not
prove inventory or therapeutic suitability.
"""
import re

CONTENT_VERSION = '2026-09-28-v2'
GYM_SOURCE = 'https://drive.google.com/file/d/1MHH8fo_EvIpnKpewGLnOXBww8VOuE6mU/view'
NF_SOURCE = 'https://drive.google.com/file/d/1oeFFexmEcPkNSjo7px32vjkiZ97misFn/view'
GUIDES = {
    'gym': {'source': GYM_SOURCE, 'reviewed_at': '2026-09-28',
            'label': 'Guantes Gym', 'method': 'mano como foto: A ancho horizontal incluyendo pulgar; B desde base de mano hasta extremo del dedo señalado',
            'owner_instruction': '2026-09-28: seguir literalmente foto A/B',
            'fit_verified': False, 'sizes_cm': {'S': [8.5, 14], 'M': [9, 15], 'L': [9.5, 16]}},
    'nf': {'source': NF_SOURCE, 'reviewed_at': '2026-09-28',
           'label': 'Guantes NF', 'method': 'circunferencia de mano justo debajo de nudillos, sin pulgar',
           'fit_verified': True, 'sizes_cm': {'S': [18, 19], 'M': [19, 20.5],
                                            'L': [20.5, 22], 'XL': [22, 23.5]}}
}
# Exact standalone listings from the existing NF mapping, not fuzzy title matching.
GYM_IDS = frozenset(('MLA2055004735', 'MLA2055004731', 'MLA2055004733', 'MLA2055004729',
    'MLA2044333081', 'MLA2044333083', 'MLA3719217048', 'MLA3719217038', 'MLA3719217042',
    'MLA3719217044', 'MLA3719217050', 'MLA3719217040', 'MLA3880322852',
    'MLA3902827472', 'MLA3902827474', 'MLA3902827470'))
NF_IDS = frozenset(('MLA1790524493', 'MLA1790512253', 'MLA1923385903', 'MLA1790499567',
                    'MLA1923220481', 'MLA1946913377', 'MLA2055006577'))
SIZE = re.compile(r'\b(talles?|tallas?|medidas?|ancho|largo|palma|pulgar|nudillos|circunferencia)\b', re.I)
HEALTH = re.compile(r'epitrocle[ií]tis|epicondilitis|tendinitis|lesi[oó]n|dolor|dol[eé]|artritis|artrosis|rehabilit|m[eé]dic|terap[eé]ut', re.I)
POSTSALE = re.compile(r'ya (?:compr[eé]|pagu[eé])|compr[eé]|mi (?:compra|pedido)|reclamo|devol|reemb|cancel|no lleg|no recib|defect|\brot[oa]\b', re.I)
TUTEO = re.compile(r'\b(tú|tu|tus|ti|contigo|tienes|puedes|necesitas|quieres|deseas|utilizas|consultas|mencionas|debes|buscas|prefieres|ten en cuenta|mide|mídete|fíjate|asegúrate)\b', re.I)
GENERIC_HELP = re.compile(r'(?:secci[oó]n|centro) de ayuda|consulta[r]? (?:el|en el) sitio|plataforma de compra', re.I)


def formal_address(question):
    return bool(re.search(r'\busted\b|\b(?:podr[ií]a|puede|tendr[ií]a|tiene|le ser[ií]a posible)\s+(?:usted\s+)?(?:indicarme|decirme|informarme|enviarme|confirmarme)|\ble consulto\b', question, re.I))


def size_guide(facts):
    # A bundle may reuse a component SKU: never apply a chart to its other products.
    title = str(facts.get('title', ''))
    if re.search(r'\bkit\b|\bcombo\b|\+', title, re.I):
        return None
    item_id = str(facts.get('id', ''))
    sku = next((str(a.get('value_name') or '') for a in facts.get('attributes', [])
                if a.get('id') == 'SELLER_SKU'), '')
    if item_id in GYM_IDS or (re.search(r'guantes?', title, re.I) and re.fullmatch(r'GMD01(?:NG|RS|MC)(?:S|M|L)', sku)):
        return GUIDES['gym']
    if item_id in NF_IDS or (re.search(r'guantes?', title, re.I) and re.fullmatch(r'GNF01(?:NG|RS)(?:S|M|L|XL)', sku)):
        return GUIDES['nf']
    return None


def closing(text):
    return '¡Hola! ' + text + ' Saludos, NorthFitness.'


def direct_answer(question, facts):
    formal = formal_address(question)
    ask = '¿Podría indicar' if formal else '¿Podés indicar'
    if HEALTH.search(question) and not POSTSALE.search(question):
        # Do not turn marketing language (or an unreviewed historical answer) into medical advice.
        attrs = {a.get('id'): a.get('value_name') for a in facts.get('attributes', [])}
        adjustment = ' La ficha indica que tiene ajuste regulable.' if attrs.get('IS_ADJUSTABLE') == 'Sí' else ''
        followup = ' ¿Qué tipo de soporte está buscando?' if formal else ' ¿Qué tipo de soporte buscás?'
        return closing('No podemos confirmar que este producto sea adecuado para esa lesión ni prometer alivio.'
                       + adjustment + followup)
    if not SIZE.search(question) or POSTSALE.search(question):
        return None
    # Stock, mixed purchases and other requests still need current listing evidence/model.
    if re.search(r'\bstock\b|\btienen\b|\bviene\b|\bdisponible\b|\bprecio\b|\benv[ií]o\b', question, re.I):
        return None
    guide = size_guide(facts)
    if not guide:
        return clarification(question, facts)
    if guide['label'] == 'Guantes Gym':
        chart = 'La tabla de Guantes Gym indica ancho × largo: S 8,5 × 14 cm; M 9 × 15 cm; L 9,5 × 16 cm.'
        method = ('Según la foto, A es el ancho horizontal de la mano incluyendo el pulgar, '
                  'entre los extremos de la línea roja; B va desde la base de la mano '
                  'hasta la punta del dedo señalado por la línea vertical. No es circunferencia.')
        followup = ('¿Qué valores de A y B obtuvo siguiendo esas líneas?' if formal else
                    '¿Qué valores de A y B obtuviste siguiendo esas líneas?')
        return closing(chart + ' ' + method + ' ' + followup)
    chart = 'La guía de Guantes NF usa circunferencia justo debajo de los nudillos, sin incluir el pulgar: S 18–19 cm; M 19–20,5 cm; L 20,5–22 cm; XL 22–23,5 cm.'
    return closing(chart + ' Los límites se superponen; la tabla no confirma disponibilidad. '
                   + ('¿Qué circunferencia obtuvo siguiendo ese método?' if formal else
                      '¿Qué circunferencia obtuviste siguiendo ese método?'))


def clarification(question, facts=None):
    formal = formal_address(question)
    if POSTSALE.search(question):
        return closing('Para revisar esa compra sin exponer datos personales, '
                       + ('escríbanos' if formal else 'escribinos') + ' desde el detalle del pedido.')
    if SIZE.search(question):
        return closing('No tenemos una guía inequívoca para recomendar un talle con los datos disponibles. '
                       + ('¿Podría indicar qué medida tomó, cómo la tomó y el valor en centímetros?' if formal else
                          '¿Podés indicar qué medida tomaste, cómo la tomaste y el valor en centímetros?'))
    if re.search(r'env[ií]o|entrega|correo|llega|retir', question, re.I):
        return closing(('Consulte' if formal else 'Consultá') + ' las opciones, el costo y la fecha estimada que Mercado Libre muestra al comprar. No podemos confirmar otro transportista.')
    if re.search(r'barra|cross\s*fit|sirve|compatible|usar|uso', question, re.I):
        return closing('Para confirmar la compatibilidad, ' +
                       ('¿qué ejercicio concreto desea realizar y con qué equipo?' if formal else
                        '¿qué ejercicio concreto querés realizar y con qué equipo?'))
    if re.search(r'\bcomprar\b|carrito|junt[oa]s?', question, re.I):
        return closing(('¿Qué productos y variantes desea combinar en la compra?' if formal else
                        '¿Qué productos y variantes querés combinar en la compra?'))
    return closing('Con los datos disponibles no podemos confirmar ese punto. ' +
                   ('¿Qué característica específica necesita verificar?' if formal else
                    '¿Qué característica específica necesitás verificar?'))


def content_issues(text, question):
    issues = []
    if TUTEO.search(text):
        issues.append('Usar registro solicitado sin tuteo ni posesivos tu/tus; no cambiar hechos.')
    if not formal_address(question) and re.search(r'\busted\b|\b(?:consulte|indique|mida|escríbanos)\b', text, re.I):
        issues.append('El comprador no inició tratamiento formal: usar voseo.')
    if formal_address(question) and re.search(r'\bvos\b|\b(?:podés|tenés|querés|necesitás|consultá|medí|estás|buscás)\b', text, re.I):
        issues.append('El comprador inició tratamiento formal: mantener usted.')
    if GENERIC_HELP.search(text) and not POSTSALE.search(question):
        issues.append('Responder el dato del producto o pedir una aclaración concreta, no derivar a ayuda genérica.')
    if len(text) > 1000 or not text.strip():
        issues.append('Respuesta entre 1 y 1000 caracteres.')
    return issues
