"""Генератор ников, похожих на настоящих игроков Black Russia.

Что было не так. Ники собирались из names.txt (18 тысяч АНГЛИЙСКИХ имён:
Jacob, Michael, Joshua) и surnames.txt (английские фамилии). На выходе
получалось «Moira_Amosov» — английское имя с русской фамилией в
транслите. Живых таких не бывает, и вся сотня аккаунтов выглядела как
одна сгенерированная пачка.

Как выглядит реальная популяция сервера (выборка из 128 ников в логах):

    Alexsandr_Markovv     русское имя + русская фамилия, удвоенная буква
    Amina_Gasanova        обычные имя и фамилия
    Arnur_Supreme         тюркское имя + английское слово
    Aleqs_Darkness        имя с опечаткой + английское слово
    Agaska_LoveMajorka    выдуманное + CamelCase-словосочетание
    Big_Shark             два английских слова
    Bober_Bobrevich       «бобёр» + псевдоотчество
    Artemetka_Lunaxov     уменьшительное + выдуманная фамилия
    Sotka_NaCapte         сленг + CamelCase-фраза

То есть это смесь: русские имена, тюркские имена, английские слова,
мемы, выдуманные фамилии, CamelCase-вставки и удвоения букв. Модуль
строит ники по этим же лекалам и в тех же пропорциях.
"""

from __future__ import annotations

import random

MAX_LEN = 20

# --- пулы -----------------------------------------------------------------

# Русские мужские имена в транслите — как их пишут в нике.
MALE_NAMES = (
    "Alexandr", "Alexey", "Andrey", "Anton", "Arkady", "Artem", "Artur",
    "Bogdan", "Boris", "Vadim", "Valentin", "Valery", "Vasily", "Victor",
    "Vitaly", "Vlad", "Vladislav", "Vladimir", "Vsevolod", "Gennady",
    "Georgy", "Gleb", "Grigory", "Danil", "Denis", "Dmitry", "Egor",
    "Zakhar", "Ivan", "Ignat", "Ilya", "Kirill", "Konstantin", "Lev",
    "Leonid", "Makar", "Maxim", "Mark", "Matvey", "Mihail", "Nikita",
    "Nikolay", "Oleg", "Pavel", "Petr", "Platon", "Rodion", "Roman",
    "Ruslan", "Saveliy", "Semyon", "Sergey", "Stanislav", "Stepan",
    "Taras", "Timofey", "Trofim", "Fedor", "Philip", "Eduard", "Yuri",
    "Yaroslav", "Sanya", "Sanek", "Dimon", "Vovan", "Kostya", "Leha",
    "Serega", "Tolya", "Zhenya", "Toha", "Gosha", "Mishan", "Vitya",
    "Slavik", "Rusik", "Danchik", "Timoha", "Edik", "Garik", "Zhora",
    "Valera", "Tolyan", "Romario", "Arturchik", "Serezhka",
)

FEMALE_NAMES = (
    "Alexandra", "Alina", "Alisa", "Alla", "Anastasia", "Angelina", "Anna",
    "Valentina", "Valeria", "Varvara", "Vasilisa", "Vera", "Veronika",
    "Victoria", "Vlada", "Daria", "Diana", "Eva", "Evgenia", "Ekaterina",
    "Elena", "Elizaveta", "Zoya", "Inna", "Irina", "Karina", "Christina",
    "Ksenia", "Larisa", "Lidia", "Lyubov", "Lyudmila", "Margarita",
    "Marina", "Maria", "Milana", "Nadezhda", "Natalia", "Nina", "Oksana",
    "Olga", "Polina", "Regina", "Rimma", "Roza", "Ruslana", "Svetlana",
    "Snezhana", "Sofia", "Stella", "Taisia", "Tamara", "Tatiana",
    "Ulyana", "Yulia", "Yana", "Nastya", "Katya", "Olya", "Yulya",
    "Dasha", "Masha", "Sonya", "Vika", "Lera", "Nika",
)

# Тюркские и кавказские имена — на BR их очень много.
TURKIC_NAMES = (
    "Arnur", "Damir", "Bimbolat", "Aza", "Bekosh", "Aslan", "Islam",
    "Magomed", "Arsen", "Alan", "Murat", "Nazar", "Eldar", "Tamerlan",
    "Ramzan", "Adam", "Amir", "Samir", "Kamil", "Ildar", "Rinat", "Airat",
    "Bulat", "Dinar", "Emil", "Marat", "Nail", "Radik", "Ravil", "Rifat",
    "Rustem", "Salavat", "Tagir", "Zufar", "Timur", "Ruslan", "Artur",
    "Ayan", "Daniyar", "Ermek", "Zhanibek", "Kairat", "Nurlan", "Sanzhar",
)

# Русские фамилии в транслите.
SURNAMES = (
    "Ivanov", "Petrov", "Smirnov", "Kuznetsov", "Popov", "Sokolov",
    "Lebedev", "Kozlov", "Novikov", "Morozov", "Volkov", "Solovyov",
    "Vasilyev", "Zaitsev", "Pavlov", "Semenov", "Golubev", "Vinogradov",
    "Bogdanov", "Vorobyov", "Fedorov", "Mihailov", "Belyaev", "Tarasov",
    "Belov", "Komarov", "Orlov", "Kiselev", "Makarov", "Andreev",
    "Kovalev", "Ilyin", "Gusev", "Titov", "Kuzmin", "Baranov", "Kulikov",
    "Alekseev", "Stepanov", "Yakovlev", "Sorokin", "Sergeev", "Romanov",
    "Zakharov", "Borisov", "Korolev", "Gerasimov", "Ponomarev",
    "Grigoriev", "Lazarev", "Medvedev", "Ershov", "Nikitin", "Sobolev",
    "Ryabov", "Polyakov", "Tsvetkov", "Danilov", "Zhukov", "Frolov",
    "Zhuravlev", "Nikolaev", "Krylov", "Maksimov", "Sidorov", "Osipov",
    "Belousov", "Fedotov", "Egorov", "Matveev", "Bobrov", "Dmitriev",
    "Kalinin", "Anisimov", "Antonov", "Filippov", "Markov", "Sukhanov",
    "Mironov", "Alexandrov", "Konovalov", "Shestakov", "Kazakov",
    "Denisov", "Gromov", "Fomin", "Davydov", "Melnikov", "Shcherbakov",
    "Kolesnikov", "Karpov", "Afanasyev", "Vlasov", "Maslov", "Isakov",
    "Tikhonov", "Lavrov", "Arkhipov", "Zotov", "Rodionov", "Savin",
    "Zubkov", "Pakhomov", "Rusakov", "Kabanov", "Burov", "Yudin",
    "Ustinov", "Trifonov", "Shilov", "Nosov", "Sazonov",
)

# Сленг и мемы в первой части — то, что просили: Sotka_..., Morgenshtern_...
SLANG_FIRST = (
    "Sotka", "Morgenshtern", "Bratan", "Chushpan", "Pahan", "Kesha",
    "Batya", "Dyadya", "Ded", "Pupsik", "Bandit", "Voron", "Groza",
    "Kaban", "Molniya", "Tihiy", "Sultan", "Shah", "Korol", "Graf",
    "Baron", "Vozhd", "Zloy", "Derzkij", "Naglyj", "Krutoy", "Delovoy",
    "Boss", "Sanych", "Mihalych", "Petrovich", "Gena", "Vovan", "Sanchous",
    "Kolyan", "Slavyan", "Tatar", "Chechen", "Kazah", "Sibiryak",
    "Kosmos", "Pistolet", "Tanec", "Pesnya", "Tancor", "Kachal",
    "Kachok", "Sportik", "Bokser", "Shtanga", "Turnik", "Kombat",
)

# Вторая часть в CamelCase — из примеров «NaCapte», «Herobrin».
SLANG_SECOND = (
    "NaCapte", "Herobrin", "VTapkah", "NaZabive", "VKedah", "NaRajone",
    "VTeme", "BezBazaru", "PoBratan", "NaPivase", "VTerle", "NaChipse",
    "ZloyDed", "BosBoss", "Krysha", "Zhest", "Pobeda", "LoveMajorka",
    "TicTak", "LoveForever", "BestPlayer", "ProGamer", "TopOne",
    "NoName", "GameOver", "DarkSide", "BlackStar", "VDele", "NaSporte",
    "VKrossah", "VZakone", "PoZhizni", "BezPonta",
    "NaRelaxe", "VAnime", "NaTune", "VStroyu", "NaVolne", "VBande",
)

# Английские слова — вторая часть у 124 из 128 реальных ников.
ENGLISH_WORDS = (
    "Shark", "Wolf", "Dragon", "Fox", "Tiger", "Eagle", "Rider", "Hunter",
    "Killer", "Ghost", "Shadow", "Storm", "Fire", "Ice", "Night", "Star",
    "King", "Lord", "Boss", "Chief", "Master", "Legend", "Hero", "Viking",
    "Knight", "Prince", "Baron", "Falcon", "Raven", "Bear", "Lion",
    "Panther", "Cobra", "Viper", "Hawk", "Bullet", "Blade", "Steel",
    "Iron", "Stone", "Thunder", "Lightning", "Phoenix", "Titan", "Ninja",
    "Samurai", "Cowboy", "Gangster", "Bandit", "Rebel", "Outlaw", "Joker",
)

# Русские основы для CamelCase-связок: NaCapte, VTapkah, PoBratan.
CAMEL_PREFIX = ("Na", "V", "Po", "Za", "Bez", "S", "Pod")
CAMEL_BASE = (
    "Capte", "Tapkah", "Zabive", "Kedah", "Rajone", "Teme", "Bazaru",
    "Bratan", "Pivase", "Terle", "Chipse", "Dele", "Sporte", "Krossah",
    "Zakone", "Zhizni", "Ponta", "Relaxe", "Anime", "Tune",
    "Stroyu", "Volne", "Bande", "Rajone", "Majore", "Tusovke", "Dvizhe",
    "Padik", "Rajku", "Kvartale", "Tachke", "Motore", "Balkone",
)

ENGLISH_FIRST = (
    "Big", "Dark", "Crazy", "Red", "Blue", "Iron", "Mad", "Silent",
    "Black", "White", "Fast", "Wild", "Cold", "Night", "Fire", "Shadow",
    "Lucky", "Golden", "Silver", "Young", "Real", "True", "Alpha", "Neo",
    "Mr", "Sir", "King", "Lord", "Top", "Pro",
)

# Выдуманные фамилии — из реальных ников: Lunaxov, Markovv, Balaboshin.
INVENTED_TAILS = (
    "xov", "ov", "ev", "in", "skiy", "enko", "uk", "yuk", "ich", "ovich",
    "ashvili", "yan", "dzhe", "zade", "ovv", "sss", "ovski", "inskij",
)

# Мемные слова-основы для выдуманных фамилий.
STEMS = (
    "Lun", "Mark", "Balabosh", "Komron", "Aret", "Plesh", "Gorbat",
    "Vatut", "Marjas", "Chekush", "Remiz", "Kovank", "Nifont", "Rasput",
    "Podoprygor", "Novikop", "Sapron", "Senektut", "Tovkun", "Cheljuk",
    "Kustod", "Shafaren", "Paramoh", "Revokat", "Smetan", "Proskur",
    "Kasil", "Alofi", "Melih", "Lumburg", "Citrus", "Bobrev", "Ibrax",
)

# --- мутации --------------------------------------------------------------


def _double_letter(word: str, rng: random.Random) -> str:
    """Удвоить букву — как Markovv, Citrusss, Lumburgsss."""
    if len(word) < 4:
        return word
    index = rng.randrange(2, len(word))
    return word[:index] + word[index] + word[index:]


def _double_last(word: str) -> str:
    """Удвоить последнюю букву — Markovv, Kovalevv, Kuznetsovv."""
    if len(word) < 3:
        return word
    return word + word[-1]


def _typo_name(word: str, rng: random.Random) -> str:
    """Имя с опечаткой — как Aleqs вместо Alex."""
    swaps = (("ks", "qs"), ("x", "qs"), ("i", "ee"), ("y", "i"), ("v", "w"))
    for old, new in swaps:
        if old in word.lower():
            return word.replace(old, new, 1)
    return word


def _camel(first: str, second: str) -> str:
    return f"{first}{second[:1].upper()}{second[1:]}"


# --- генератор ------------------------------------------------------------


class NickGenerator:
    """Собирает ники в пропорциях, похожих на живую популяцию сервера."""

    # Веса стилей. Русские имена и фамилии — основа, мемы и английский —
    # заметная, но не главная часть, иначе сотня ботов выглядит как один
    # чат с приколами.
    STYLES = (
        ("real", 38),
        ("meme", 18),
        ("english", 14),
        ("mixed", 14),
        ("invented", 10),
        ("short", 6),
    )

    def __init__(self, rng: random.Random | None = None, max_len: int | None = None):
        self.rng = rng or random.Random()
        self._used: set[str] = set()
        # Лимит длины у проекта свой. По умолчанию — общий MAX_LEN, но
        # Arizona RP может урезать ник сильнее, и обрезать его должен
        # генератор, а не сервер после отказа в регистрации.
        try:
            value = int(max_len) if max_len else MAX_LEN
        except (TypeError, ValueError):
            value = MAX_LEN
        self.max_len = value if value > 4 else MAX_LEN

    # --- отдельные стили ---

    def _pick_name(self) -> str:
        rng = self.rng
        pool = rng.choices(
            (MALE_NAMES, FEMALE_NAMES, TURKIC_NAMES),
            weights=(50, 25, 25),
        )[0]
        return rng.choice(pool)

    def _pick_name_and_surname(self) -> tuple[str, str, bool]:
        """Имя, фамилия и признак женского имени.

        Фамилию возвращаем ещё без женского окончания: удвоение буквы
        должно идти ДО него, иначе получается «Shilovaa» вместо
        «Shilova» — удваивается само окончание.
        """
        rng = self.rng
        pool = rng.choices(
            (MALE_NAMES, FEMALE_NAMES, TURKIC_NAMES),
            weights=(50, 25, 25),
        )[0]
        name = rng.choice(pool)
        surname = rng.choice(SURNAMES)
        return name, surname, pool is FEMALE_NAMES

    def _real(self) -> str:
        """Amina_Gasanova, Alexsandr_Markovv."""
        name, surname, female = self._pick_name_and_surname()
        if self.rng.random() < 0.12:
            surname = _double_letter(surname, self.rng)
        if female and self.rng.random() < 0.85:
            # Женская фамилия с «-а»: Amina_Gasanova, а не Amina_Gasanov.
            if not surname.endswith(("a", "ya")):
                surname += "a"
        if self.rng.random() < 0.10:
            name = _typo_name(name, self.rng)
        return f"{name}_{surname}"

    def _meme(self) -> str:
        """Sotka_NaCapte, Morgenshtern_Herobrin."""
        first = self.rng.choice(SLANG_FIRST)
        if self.rng.random() < 0.55:
            second = self.rng.choice(SLANG_SECOND)
        else:
            # Связка из русских основ: NaCapte, VTapkah, PoBratan.
            # Английское слово сюда не годится — получается «ZaPrince»,
            # а так на сервере не пишут.
            second = _camel(
                self.rng.choice(CAMEL_PREFIX),
                self.rng.choice(CAMEL_BASE),
            )
        return f"{first}_{second}"

    def _english(self) -> str:
        """Big_Shark, Dark_Wolf."""
        return f"{self.rng.choice(ENGLISH_FIRST)}_{self.rng.choice(ENGLISH_WORDS)}"

    def _mixed(self) -> str:
        """Arnur_Supreme, Aleqs_Darkness."""
        name = self._pick_name()
        if self.rng.random() < 0.15:
            name = _typo_name(name, self.rng)
        return f"{name}_{self.rng.choice(ENGLISH_WORDS)}"

    def _invented(self) -> str:
        """Artemetka_Lunaxov, Bober_Bobrevich."""
        if self.rng.random() < 0.5:
            first = self.rng.choice(SLANG_FIRST)
        else:
            first = self._pick_name()
        surname = self.rng.choice(STEMS) + self.rng.choice(INVENTED_TAILS)
        return f"{first}_{surname}"

    def _short(self) -> str:
        """Aro_Bos, Caha_Carter — короткие и простые."""
        first = self.rng.choice(("Aro", "Caha", "Bek", "Aza", "Dan", "Nik",
                                 "Max", "Leo", "Tim", "Sam", "Ray", "Kai"))
        second = self.rng.choice(("Bos", "Carter", "Ray", "Kane", "Cole",
                                  "Ford", "Hunt", "Wolf", "King", "Stone"))
        return f"{first}_{second}"

    # --- сборка ---

    def _style(self) -> str:
        names = [name for name, _weight in self.STYLES]
        weights = [weight for _name, weight in self.STYLES]
        return self.rng.choices(names, weights=weights)[0]

    def build(self) -> str:
        """Один ник нужного стиля, обрезанный до лимита игры."""
        style = self._style()
        nick = getattr(self, f"_{style}")()
        if len(nick) > self.max_len:
            # Обрезаем вторую часть, а не первую: имя должно остаться целым.
            first, _, second = nick.partition("_")
            room = self.max_len - len(first) - 1
            if room >= 3:
                nick = f"{first}_{second[:room]}"
            else:
                nick = nick[:self.max_len]
        return nick

    def generate(self, avoid=None, attempts: int = 400) -> str:
        """Уникальный ник, не пересекающийся с avoid и уже выданными.

        Цифр в нике не бывает ни при каких обстоятельствах. Раньше, если
        подобрать не удавалось, к фамилии приклеивался номер — получалось
        «Aro_Carter2». На сервере так не пишут: цифра в нике выдаёт
        сгенерированный аккаунт с первого взгляда.
        """
        avoid = set(avoid or ()) | self._used
        for _ in range(attempts):
            nick = self.build()
            if nick not in avoid:
                self._used.add(nick)
                return nick

        # Фолбэк без цифр: удваиваем последнюю букву фамилии — ровно так
        # делают живые игроки (Markovv, Kovalevv, Kuznetsovv).
        base = self.build()
        first, _, second = base.partition("_")
        room = self.max_len - len(first) - 1

        for _ in range(12):
            doubled = _double_last(second)
            # «Antonova» + «a» = «Antonovaa» — так не пишут. Удваиваем
            # предпоследнюю букву, если фамилия уже кончается на «-a».
            if doubled.endswith("aa"):
                doubled = second[:-1] + second[-2] + second[-1]
            candidate = f"{first}_{doubled[:room]}"
            if candidate not in avoid:
                self._used.add(candidate)
                return candidate

        # И совсем крайний случай — буквенные хвосты, которыми тоже
        # пользуются вживую: Sanek, Sanekx, Sanekov.
        for tail in ("x", "s", "z", "ik", "ek", "ov", "on", "en"):
            candidate = f"{first}_{(second + tail)[:room]}"
            if candidate not in avoid:
                self._used.add(candidate)
                return candidate

        raise RuntimeError("не удалось подобрать уникальный ник")
