"""Small built-in lists of common first names and common surnames (Miami-Dade
mix: Hispanic + Anglo + Haitian), used for two cheap, free guesses that never
cost a FOREWARN search:

1. `looks_reversed(first, last)` -- true only when the "first" word is clearly a
   surname and the "last" word is clearly a first name ("Acevedo Ximena").
   The Owner Lookup review step pre-swaps such rows (Mario can still click to
   undo), so a reversed row is fixed before a search is spent on it.
2. `is_common_first_name(word)` -- so a plain middle name ("SMITH JOHN
   MICHAEL") isn't mistaken for a second surname.

Deliberately conservative: a word that appears in BOTH lists (Santiago,
Jordan, Marin, ...) is treated as unknown, and a swap is only suggested when
both words are confidently on the "wrong" side. Anything not on a list is
simply "unknown" and never triggers anything."""

_FIRST = """
aaron abel abigail abraham adriana adrian adrienne agustin alain alan albert alberto alejandra alejandro alex alexander alexandra alexis alfonso alfredo alice alicia alina alison allan alma alvaro amanda amelia amy ana andrea andres andrew angel angela angelica angelo anibal anita ann anna anne annette anthony antonia antonio arelis ariel armando arnaldo arturo ashley augusto barbara beatriz benjamin berta bertha betty bianca blanca bob bobby brandon brenda brian bruce camila camilo candida carla carlos carmen carol carolina caroline cecilia celia celina cesar charles charlie chris christian christina christopher cindy claudia claudio clara clarice clifford consuelo cristina cristobal cynthia daisy dalia damian dana daniel daniela danny darío dario david deborah debra delia denise dennis diana diego dolores dominic donald donna dora doris dorothy douglas eddy eddie edgar eduardo edward edwin egildo eileen elena eliana elias elisa eliseo eliza elizabeth ella ellen elsa elvira emilia emilio emily emma enrique eric erica erik erika ernesto esperanza esteban estela estella esther eugenia eugenio eva evelyn evelio fabian fabiola felipe felix fernando flor florencia francesca francis francisca francisco frank fred freddy gabriel gabriela gary gene george georgina gerald geraldine gerardo gilberto gina giovanni gladys gloria gonzalo grace graciela gregorio gregory guadalupe guillermo gustavo harry hector heidi helen helena henry herbert herman hernan hilda hilario hugo humberto ian ignacio ileana imelda ines irene iris isabel isabella isaac isidro ismael ivan ivette jacqueline jaime james jane janet janice jason javier jean jeanette jeffrey jennifer jenny jeremy jerome jesse jessica jesus jill joan joaquin joe joel john johnny jonathan jorge jose josefa josefina joseph josue joy juan juana judith julia julian juliana julio justin karen karina katherine kathleen kathy keith kelly ken kenneth kevin kim kimberly kyle larry laura lazaro lea leandro leila lena leo leon leonardo leonor leticia lidia liliana lilian lillian linda lionel lisa liz lorena lorenzo loretta lori lorraine louis louise lourdes lucas lucia luciano lucila lucy luis luisa luz lydia magdalena manuel mara marcela marcelo marcia marco marcos margarita maria mariana marian mariano maribel marie marilyn marina mario marisol marissa marjorie mark marlene marta martha martin mary mateo matthew mauricio maureen max maximo megan melanie melinda melissa mercedes michael michelle miguel mildred milagros miriam miryam mirta mitchell monica myriam nadia nancy natalia natalie natasha nathan nelson nestor nicholas nicolas nidia nilda noel nora norma norman olga olivia omar orlando oscar osvaldo pablo pamela paola patricia patrick paul paula paulina pedro peggy peter philip pilar rachel rafael ralph ramiro ramon raquel raul rebeca rebecca regina reinaldo renata rene reynaldo ricardo richard rigoberto rita robert roberto rocio rodolfo rodrigo roger rolando roman ronald rosa rosalia rosario rosemary rosendo ross roxana ruben ruth ryan salvador samantha samuel sandra sandy santiago sara sarah saul sebastian sergio sharon sheila shirley silvia silvio simon sofia sonia stanley stephanie stephen steve steven susan susana suzanne sylvia tamara tania teresa terry theresa thimoclea thomas timothy tina tomas tony ulises ursula valentina valeria vanessa velma vera veronica vicente victor victoria viviana vivian walter wanda wayne wendy wilfredo william wilson xavier ximena yadira yamile yanet yaniris yesenia yolanda yoli yunierka yves zoila
""".split()

_SURNAMES = """
acevedo acosta aguilar aguirre alarcon alba albert alfonso alonso alvarado alvarez amador andrade angulo aragon aranda arango arce arcos arias armas arroyo avila ayala baez barrios barrera batista bautista bello benitez bermudez bernal blanco bravo bustamante caballero cabezas cabrera calderon camacho campos cano cardenas cardona carrasco carrillo casas castellanos castillo castro ceballos cepeda cervantes chavez cisneros collazo colon contreras cordero cordova corona correa cortes cruz cuellar cuevas delgado diaz dominguez duarte duran echevarria escobar espinosa espinoza esquivel estrada fernandez ferrer figueroa flores fonseca franco fuentes galindo gallardo gallego garcia garrido garza gil gomez gonzalez granados guerra guerrero gutierrez guzman henriquez heredia hernandez herrera hidalgo hinojosa huerta ibarra jaramillo jimenez juarez lara leal ledesma leon lezama lopez lorenzo lozano lugo lujan luna macias maldonado marin marquez marrero martinez mata medina mejia mena mendez mendoza menendez mercado mesa miranda mojica molina montalvo montes montoya mora morales moreno munoz naranjo navarro nieves nunez ochoa olivares oliva ordonez orozco ortega ortiz osorio pacheco padilla paez palacios pardo paredes pena perales perez pineda pinto pizarro ponce portillo prieto puentes quesada quintana quintero quiroz ramirez ramos rangel rey reyes rios rivas rivera rivero robles rocha rodriguez rojas roldan romero rosales rosario ruiz saavedra salas salazar saldana salinas sanchez sandoval santana santos sepulveda serrano sierra silva solis soto suarez tapia tejada toro torres trejo trujillo valdes valdez valencia valenzuela vargas vasquez vazquez vega velasquez velazquez velez venegas vera vidal villa villalobos villanueva zamora zapata zavala zuniga
adams allen anderson bailey baker barnes bell bennett brooks brown bryant butler campbell carter clark coleman collins cook cooper cox davis edwards ellis evans fisher foster gibson graham grant gray green griffin hall hamilton harris hayes henderson hill howard hughes jackson james jenkins johnson jones kelly kennedy king lee lewis long martin miller mitchell moore morgan morris murphy nelson parker patterson perry peterson phillips powell price reed richardson roberts robinson rogers ross russell sanders scott simmons smith stewart sullivan taylor thomas thompson turner walker ward washington watson webb wells west white williams wilson wood wright young
altshuler amador batista cleveland dessources estime etienne exantus francois jean jeanbaptiste joseph louis noel pierre saint toussaint
""".split()

FIRST_NAMES = set(_FIRST)
SURNAMES = set(_SURNAMES)


def _key(word: str) -> str:
    import unicodedata
    w = unicodedata.normalize("NFKD", (word or "").lower())
    return "".join(c for c in w if c.isalpha())


def is_common_first_name(word: str) -> bool:
    return _key(word) in FIRST_NAMES


def looks_reversed(first: str, last: str) -> bool:
    """True only when `first` is clearly a surname and `last` is clearly a
    first name. A word that's on both lists, or on neither, never counts."""
    f, l = _key(first), _key(last)
    if not f or not l or " " in (first or "").strip() or " " in (last or "").strip():
        return False
    f_is_surname = f in SURNAMES and f not in FIRST_NAMES
    l_is_first = l in FIRST_NAMES and l not in SURNAMES
    return f_is_surname and l_is_first
