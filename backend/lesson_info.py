from __future__ import annotations

import abc
import dataclasses
import datetime
import logging
import re
import typing
import spacy  # spacy for language analysis

from .vplan_utils import (
    parse_periods, _parse_form_pattern, ParsedForm, parsed_forms_to_str, forms_to_str,
    _loose_parse_form_pattern
)
from . import teacher as teacher_model, blocks, typography_fixer
from . import models

nlp = spacy.load("de_core_news_sm")  # load german language model


# Nicht verfügbare Räume:	1302 (1-2,7-10), 1306 (1-2,4,6)


class _InfoParsers:
    # TODO: this strict name parsing may not be necessary
    _name_element = r"(?:[A-ZÄÖÜ]')?[A-ZÄÖÜ][a-zäöüß]+(?:-[A-ZÄÖÜ][a-zäöüß]+)*\.?"
    _teacher_name = (
        # title
        fr"(?:{_name_element})"
        # name
        fr"(?: ?{_name_element})*"
        r"(?: van)?"
        fr"(?: {_name_element})"
    )
    _teacher_abbreviation = r"[A-ZÄÖÜ][A-ZÄÖÜa-zäöüß]{2,}"
    _teacher = fr"(?:{_teacher_name})|(?:{_teacher_abbreviation})"

    # teacher a,teacher b
    _teachers = fr"{_teacher}(?:, ?{_teacher})*"
    _course = r"([A-Za-z0-9ÄÖÜäöüß\/\-_]{2,8})"  # maybe be more strict?
    _period = r"St\.(?P<periods>(?P<period_begin>\d{1,2})(?:-(?P<period_end>\d{1,2}))?)"
    _form = _parse_form_pattern.pattern

    _weekday = r"(?:Mo|Di|Mi|Do|Fr|Sa|So)"
    _date = r"(?:\d{2}\.\d{2}\.)"

    _lesson_specifier = fr"{_weekday} \((?P<date>{_date})\) {_period}"

    # SUBSTITUTION (current lesson)
    # verlegt von St.7
    moved_from = re.compile(rf'verlegt von {_period}')

    # course is always a subject
    substitution = re.compile(rf'für (?P<course>{_course}) (?P<teachers>{_teachers})')

    # What Happens With The Substituted Lesson
    # statt Mi (07.06.) St.1-2
    # statt Mo (05.06.) St.1-2
    # statt Mo (05.06.) St.5-6
    # statt Di (06.06.) St.7-8
    instead_of = re.compile(rf'statt {_weekday} \((?P<date>{_date})\) {_period}')

    # DE Frau Musterfrau gehalten am Mo (05.06.) St.1-2
    # GEO Frau Musterfrau gehalten am Mo (05.06.) St.5-6
    held_at = re.compile(rf'(?P<course>{_course}) (?P<teachers>{_teachers}) gehalten am {_lesson_specifier}')

    # MA Frau Musterfrau verlegt nach St.3
    # EN Frau Musterfrau verlegt nach St.5-6
    # GE Frau Musterfrau verlegt nach Do (08.06.) St.3-4
    moved_to = re.compile(rf'(?P<course>{_course}) (?P<teachers>{_teachers}) verlegt nach {_period}')
    moved_to_date = re.compile(rf'(?P<course>{_course}) (?P<teachers>{_teachers}) verlegt nach {_lesson_specifier}')

    # CANCELLED
    # SPO Herr Mustermann fällt aus
    # 11spo3 Frau Musterfrau fällt aus
    # GRW Frau Musterfrau fällt aus
    # 10Et12 Frau Musterfrau fällt aus
    cancelled = re.compile(rf'(?P<course>{_course}) (?P<teachers>{_teachers}) (fällt aus|entfällt)')

    # Not Quite Cancelled
    # selbst. (v), Aufgaben stehen im LernSax, bitte in der Bibo bearbeiten
    # selbst. (v), Aufgaben stehen im LernSax, bitte zu Hause bearbeiten
    # selbst. (v), Aufgaben stehen im LernSax
    # selbst. (v), Aufgaben wurden erteilt, bitte zu Hause erledigen
    independent = re.compile(rf'selbst\.(?: \(.\))?')

    tasks_in_lernsax = re.compile(rf'Aufgaben stehen im LernSax')
    tasks_were_given = re.compile(rf'Aufgaben wurden erteilt')

    do_where = re.compile(rf'bitte(( \w+)+) bearbeiten')  # 1. group: where

    # gesamte Klasse 6/2
    whole_form = re.compile(rf'gesamte Klasse (?P<form>{_form})')

    # individuelle Nachbearbeitung des aktuellen Stoffes in der Bibo bzw. 10/1 zu Hause
    individual_revision = re.compile(rf'individuelle Nachbearbeitung des aktuellen Stoffes (?P<location>in der Bibo)?')

    # Exams
    # Prüfung Nachname
    exam = re.compile(rf'Prüfung (?P<last_name>[A-ZÄÖÜ][a-zäöüß]+)')

    # Aufsicht Vorbereitungsraum mündliche Prüfung

    """
    eigentliche Stunde findet nicht statt, weil sie auf andere Stunde verlegt wurde
    "verlegt in die Vergangenheit"          =: "gehalten am"
    "verlegt in die Zukunft"                =: "verlegt nach"
    
    eigentliche Stunde findet nicht statt, weil andere Stunde auf diese verlegt wurde 
    "verlegt aus der Zukunft/Vergangenheit" =: "statt"
    "verlegt aus der Zukunft/Vergangenheit" =: "verlegt von" (gleicher Tag)
    """


class SerializeMixin:
    def serialize(self: dataclasses.dataclass) -> dict[str, typing.Any]:
        def convert(obj: typing.Any) -> typing.Any:
            if isinstance(obj, datetime.date):
                return obj.isoformat()
            elif isinstance(obj, set):
                return sorted(obj)
            else:
                return obj

        return {
            "type": getattr(self, "__serialize_type__", self.__class__.__name__),
            **{k: convert(v) for k, v in dataclasses.asdict(self).items()}
        }


def de_weekday_to_str(weekday: int) -> str:
    return [
        "Mo",
        "Di",
        "Mi",
        "Do",
        "Fr",
        "Sa",
        "So"
    ][weekday]


class ParsedLessonInfoMessage(abc.ABC):
    before: str = ""
    after: str = ""
    plan_type: typing.Literal["forms", "teachers", "rooms", None]
    original_messages: list[str] = None

    @abc.abstractmethod
    def serialize(self) -> dict[str, typing.Any]:
        ...

    def to_text_segments(self, lesson_date: datetime.date,
                         block_config: blocks.BlockConfiguration) -> list[LessonInfoTextSegment]:
        return [
            *([LessonInfoTextSegment(self.before)] if self.before else []),
            *self._to_text_segments(lesson_date, block_config),
            *([LessonInfoTextSegment(self.after)] if self.after else [])
        ]

    def _to_text_segments(self, lesson_date: datetime.date,
                          block_config: blocks.BlockConfiguration) -> list[LessonInfoTextSegment]:
        return [
            LessonInfoTextSegment("\n".join(self.original_messages))
        ]


@dataclasses.dataclass
class MovedFrom(SerializeMixin, ParsedLessonInfoMessage):
    plan_type: typing.Literal["forms", "teachers", "rooms"]
    plan_value: set[str]
    periods: set[int]
    date: datetime.date | None

    def _to_text_segments(self, lesson_date: datetime.date,
                          block_config: blocks.BlockConfiguration) -> list[LessonInfoTextSegment]:
        if self.date is None:
            return [
                LessonInfoTextSegment("verlegt von "),
                LessonInfoTextSegment(
                    block_config.get_label_of_periods(self.periods),
                    link=LessonInfoTextSegmentLink(
                        type=self.plan_type,
                        value=sorted(self.plan_value),
                        date=lesson_date,
                        periods=sorted(self.periods)
                    )
                )
            ]
        else:
            return [
                LessonInfoTextSegment("statt "),
                LessonInfoTextSegment(
                    f"{de_weekday_to_str(self.date.weekday())} ({self.date.strftime('%d.%m.%Y')}) "
                    f"{block_config.get_label_of_periods(self.periods)}",
                    link=LessonInfoTextSegmentLink(
                        type=self.plan_type,
                        value=sorted(self.plan_value),
                        date=self.date,
                        periods=sorted(self.periods)
                    )
                )
            ]

    def is_groupable(self, other: MovedFrom):
        return (
            self.before == other.before and
            self.after == other.after and
            self.date == other.date and
            self.plan_type == other.plan_type
        )


@dataclasses.dataclass
class AbstractParsedLessonInfoMessageWithCourseInfo(ParsedLessonInfoMessage, abc.ABC):
    course: str
    # _teachers: list[str]
    # teachers: list[str] = dataclasses.field(init=False, default=None)

    plan_type: typing.Literal["forms", "teachers", "rooms"]
    # other info to display:  teacher  form
    plan_value: set[str]

    other_info_value: set[str] | None  # None if teacher abbreviations are not yet resolved

    _teachers: set[str] = dataclasses.field(default=None, kw_only=True)

    @staticmethod
    def get_other_info_type_of_plan_type(plan_type: typing.Literal["forms", "teachers", "rooms"]
                                         ) -> typing.Literal["forms", "teachers", "rooms"]:
        return {"forms": "teachers", "teachers": "forms", "rooms": "forms"}[plan_type]

    @property
    def other_info_type(self):
        return self.get_other_info_type_of_plan_type(self.plan_type)

    @property
    def original_other_info_value(self):
        if self._teachers:
            return self._teachers
        else:
            return self.other_info_value

    def _course_text_segments(self, date: datetime.date, periods: list[int] | None) -> list[LessonInfoTextSegment]:
        if self.other_info_type == "forms":
            other_info_value_text = forms_to_str(self.other_info_value)
        else:
            other_info_value_text = ', '.join(self.original_other_info_value)

        return [
            LessonInfoTextSegment(f"{self.course} "),
            LessonInfoTextSegment(
                other_info_value_text,
                link=LessonInfoTextSegmentLink(
                    type=self.other_info_type,
                    value=sorted(self.other_info_value),
                    date=date,
                    periods=periods
                ) if self.other_info_value else None
            )
        ]


@dataclasses.dataclass
class MovedTo(SerializeMixin, AbstractParsedLessonInfoMessageWithCourseInfo):
    date: datetime.date | None
    periods: set[int]

    def _to_text_segments(self, lesson_date: datetime.date,
                          block_config: blocks.BlockConfiguration) -> list[LessonInfoTextSegment]:
        if self.date is None:
            return [
                *self._course_text_segments(lesson_date, None),
                LessonInfoTextSegment(" verlegt nach "),
                LessonInfoTextSegment(
                    block_config.get_label_of_periods(self.periods),
                    link=LessonInfoTextSegmentLink(
                        type=self.plan_type,
                        value=sorted(self.plan_value),
                        date=lesson_date,
                        periods=sorted(self.periods)
                    )
                )
            ]
        elif self.date < lesson_date:
            return [
                *self._course_text_segments(self.date, sorted(self.periods)),
                LessonInfoTextSegment(" gehalten am "),
                LessonInfoTextSegment(
                    f"{de_weekday_to_str(self.date.weekday())} ({self.date.strftime('%d.%m.%Y')}) "
                    f"{block_config.get_label_of_periods(self.periods)}",
                    link=LessonInfoTextSegmentLink(
                        type=self.plan_type,
                        value=sorted(self.plan_value),
                        date=self.date,
                        periods=sorted(self.periods)
                    )
                )
            ]
        else:
            return [
                *self._course_text_segments(self.date, sorted(self.periods)),
                LessonInfoTextSegment(" verlegt nach "),
                LessonInfoTextSegment(
                    f"{de_weekday_to_str(self.date.weekday())} ({self.date.strftime('%d.%m.%Y')}) "
                    f"{block_config.get_label_of_periods(self.periods)}",
                    link=LessonInfoTextSegmentLink(
                        type=self.plan_type,
                        value=sorted(self.plan_value),
                        date=self.date,
                        periods=sorted(self.periods)
                    )
                )
            ]

    def is_groupable(self, other: MovedTo):
        return (
            self.course == other.course and
            self.original_other_info_value == other.original_other_info_value and
            self.date == other.date and
            self.plan_type == other.plan_type
        )


@dataclasses.dataclass
class InsteadOfCourse(SerializeMixin, AbstractParsedLessonInfoMessageWithCourseInfo):
    periods: set[int]

    def _to_text_segments(self, lesson_date: datetime.date,
                          block_config: blocks.BlockConfiguration) -> list[LessonInfoTextSegment]:
        return [
            LessonInfoTextSegment(f"für "),
            *self._course_text_segments(lesson_date, sorted(self.periods)),
        ]


@dataclasses.dataclass
class Cancelled(SerializeMixin, AbstractParsedLessonInfoMessageWithCourseInfo):
    periods: set[int]

    def _to_text_segments(self, lesson_date: datetime.date,
                          block_config: blocks.BlockConfiguration) -> list[LessonInfoTextSegment]:
        return [
            *self._course_text_segments(lesson_date, sorted(self.periods)),
            LessonInfoTextSegment(" fällt aus")
        ]


@dataclasses.dataclass
class DoIndependently(SerializeMixin, ParsedLessonInfoMessage):
    plan_type: str = dataclasses.field(init=False, default="forms")


@dataclasses.dataclass
class TasksInLernsax(SerializeMixin, ParsedLessonInfoMessage):
    plan_type: str = dataclasses.field(init=False, default="forms")


@dataclasses.dataclass
class TasksWereGiven(SerializeMixin, ParsedLessonInfoMessage):
    plan_type: str = dataclasses.field(init=False, default="forms")


@dataclasses.dataclass
class DoAtLocation(SerializeMixin, ParsedLessonInfoMessage):
    location: str
    plan_type: str = dataclasses.field(init=False, default="forms")


@dataclasses.dataclass
class IndividualRevision(SerializeMixin, ParsedLessonInfoMessage):
    location: str | None
    plan_type: str = dataclasses.field(init=False, default="forms")


@dataclasses.dataclass
class Exam(SerializeMixin, ParsedLessonInfoMessage):
    last_name: str
    plan_type = None


@dataclasses.dataclass
class WholeForm(SerializeMixin, ParsedLessonInfoMessage):
    form: str
    periods: set[int]
    plan_type = None

    def _to_text_segments(self, lesson_date: datetime.date,
                          block_config: blocks.BlockConfiguration) -> list[LessonInfoTextSegment]:
        return [
            LessonInfoTextSegment(f"gesamte "),
            LessonInfoTextSegment(
                f"Klasse {self.form}",
                link=LessonInfoTextSegmentLink(
                    type="forms",
                    value=[self.form],
                    date=lesson_date,
                    periods=sorted(self.periods)
                )
            )
        ]


@dataclasses.dataclass
class FailedToParse(SerializeMixin, ParsedLessonInfoMessage):
    plan_type = None

    def _to_text_segments(self, lesson_date: datetime.date,
                          block_config: blocks.BlockConfiguration) -> list[LessonInfoTextSegment]:
        return [
            LessonInfoTextSegment(typography_fixer.fix_typography(", ".join(self.original_messages)))
        ]


def create_literal_parsed_info(msg: str) -> ParsedLessonInfo:
    obj = FailedToParse()
    obj.original_messages = [msg]
    obj.before = ""
    obj.after = ""

    return ParsedLessonInfo(
        paragraphs=[
            LessonInfoParagraph(
                messages=[
                    LessonInfoMessage(
                        parsed=obj,
                        index=0
                    )
                ],
                index=0
            )
        ]
    )


def _parse_form_plan_message(info: str, lesson: models.Lesson) -> tuple[ParsedLessonInfoMessage, re.Match | None]:
    if match := _InfoParsers.substitution.match(info):
        return InsteadOfCourse(
            plan_type="forms",
            plan_value=lesson.forms,
            course=match.group("course"),
            other_info_value=None,
            periods=lesson.periods,
            _teachers=set(match.group("teachers").split(",")),
        ), match
    elif match := _InfoParsers.moved_from.match(info):
        return MovedFrom(
            plan_type="forms",
            plan_value=lesson.forms,
            periods={int(match.group("period_begin"))},
            date=None
        ), match
    elif match := _InfoParsers.instead_of.match(info):
        return MovedFrom(
            plan_type="forms",
            plan_value=lesson.forms,
            periods=parse_periods(match.group("periods")),
            date=datetime.datetime.strptime(f'{match.group("date")}{lesson._lesson_date.year}', "%d.%m.%Y").date()
        ), match
    elif match := _InfoParsers.held_at.match(info):
        return MovedTo(
            plan_type="forms",
            plan_value=lesson.forms,
            course=match.group("course"),
            _teachers=set(match.group("teachers").split(",")),
            other_info_value=None,
            date=datetime.datetime.strptime(f'{match.group("date")}{lesson._lesson_date.year}', "%d.%m.%Y").date(),
            periods=parse_periods(match.group("periods"))
        ), match
    elif match := _InfoParsers.moved_to.match(info):
        return MovedTo(
            plan_type="forms",
            plan_value=lesson.forms,
            course=match.group("course"),
            _teachers=set(match.group("teachers").split(",")),
            other_info_value=None,
            date=None,
            periods=parse_periods(match.group("periods")),
        ), match
    elif match := _InfoParsers.moved_to_date.match(info):
        return MovedTo(
            plan_type="forms",
            plan_value=lesson.forms,
            course=match.group("course"),
            _teachers=set(match.group("teachers").split(",")),
            other_info_value=None,
            date=datetime.datetime.strptime(f'{match.group("date")}{lesson._lesson_date.year}', "%d.%m.%Y").date(),
            periods=parse_periods(match.group("periods")),
        ), match
    elif match := _InfoParsers.cancelled.match(info):
        return Cancelled(
            plan_type="forms",
            plan_value=lesson.forms,
            course=match.group("course"),
            _teachers=set(match.group("teachers").split(",")),
            other_info_value=None,
            periods=lesson.periods,
        ), match
    elif match := _InfoParsers.exam.search(info):
        return Exam(match.group("last_name")), match
    elif match := _InfoParsers.do_where.search(info):
        return DoAtLocation(match.group(1).strip()), match
    elif match := _InfoParsers.individual_revision.search(info):
        return IndividualRevision(match.groupdict(None)["location"]), match
    elif match := _InfoParsers.whole_form.search(info):
        return WholeForm(
            form=match.group("form").replace(" ", ""),
            periods=lesson.periods
        ), match
    elif match := _InfoParsers.independent.search(info):
        return DoIndependently(), match
    elif match := _InfoParsers.tasks_in_lernsax.search(info):
        return TasksInLernsax(), match
    elif match := _InfoParsers.tasks_were_given.search(info):
        return TasksWereGiven(), match
    else:
        return FailedToParse(), None


def _parse_message(info: str, lesson: models.Lesson, plan_type: typing.Literal["forms", "teachers", "rooms"]
                   ) -> ParsedLessonInfoMessage:
    info = info.strip()
    info = re.sub(r"(?<=\w)/ ", "/", info)  # remove spaces after slashes like in G/ R/ W

    if plan_type == "forms":
        parsed_info, match = _parse_form_plan_message(info, lesson)
    else:
        # TODO
        parsed_info = FailedToParse()
        match = None

    if match is not None:
        parsed_info.original_messages = [info[match.start():match.end()]]
        parsed_info.before = info[:match.start()]
        parsed_info.after = info[match.end():]
    else:
        parsed_info.original_messages = [info]
        parsed_info.before = ""
        parsed_info.after = ""

    return parsed_info


@dataclasses.dataclass
class LessonInfoTextSegmentLink(SerializeMixin):
    type: typing.Literal["forms", "teachers", "rooms"]
    value: list[str]
    date: datetime.date | None
    periods: list[int] | None

    @property
    def __serialize_type__(self):
        return self.type


@dataclasses.dataclass
class LessonInfoTextSegment:
    text: str
    link: LessonInfoTextSegmentLink | None = None

    def serialize(self) -> dict:
        return {
            "text": self.text,
            "link": self.link.serialize() if self.link is not None else None
        }


@dataclasses.dataclass
class LessonInfoMessage:
    parsed: ParsedLessonInfoMessage
    index: int

    @classmethod
    def from_str(cls, message: str, lesson: models.Lesson, index: int,
                 plan_type: typing.Literal["forms", "teachers", "rooms"]) -> LessonInfoMessage:
        parsed = _parse_message(message, lesson, plan_type)

        return cls(
            parsed=parsed,
            index=index
        )

    def serialize(self, lesson_date: datetime.date, block_config: blocks.BlockConfiguration) -> dict:
        return {
            "parsed": (
                self.parsed.serialize()
                if not isinstance(self.parsed, FailedToParse) else None
            ),
            "text_segments": [
                segment.serialize() for segment in self.parsed.to_text_segments(lesson_date, block_config)
            ]
        }


def split_parens_aware(string: str, sep: str, parens: dict[str, str] = None) -> list[str]:
    assert len(sep) == 1

    parens = {"(": ")"} if parens is None else parens

    parens_stack: list[str] = []
    sep_indexes: list[int] = []

    for i, char in enumerate(string):
        if char in parens:
            parens_stack.append(parens[char])
        elif parens_stack and char == parens_stack[-1]:
            parens_stack.pop()
        elif char == sep and not parens_stack:
            sep_indexes.append(i)

    out = []
    start_index = 0
    for sep_index in sep_indexes:
        out.append(string[start_index:sep_index])
        start_index = sep_index + len(sep)

    out.append(string[start_index:])

    return out


@dataclasses.dataclass
class LessonInfoParagraph:
    messages: list[LessonInfoMessage]
    index: int

    @classmethod
    def from_str(cls, paragraph: str, lesson: models.Lesson, index: int,
                 plan_type: typing.Literal["forms", "teachers", "rooms"]) -> LessonInfoParagraph:
        messages = [
            LessonInfoMessage.from_str(message.strip(), lesson, i, plan_type)
            for i, message in enumerate(split_parens_aware(paragraph, ",")) if message.strip()
        ]
        new_messages = []
        for message in messages:
            can_merge = (
                new_messages
                and isinstance(new_messages[-1].parsed, FailedToParse)
                and isinstance(message.parsed, FailedToParse)
            )
            if can_merge:
                new_messages[-1].parsed.original_messages += message.parsed.original_messages
            else:
                new_messages.append(message)

        return cls(new_messages, index=index)

    def serialize(self, lesson_date: datetime.date, block_config: blocks.BlockConfiguration) -> list:
        return [info.serialize(lesson_date, block_config) for info in self.messages]

    def sorted(self, key: typing.Callable[[LessonInfoMessage], typing.Any]) -> LessonInfoParagraph:
        return LessonInfoParagraph(sorted(self.messages, key=key), self.index)

    def sort(self, key: typing.Callable[[LessonInfoMessage], typing.Any]):
        self.messages.sort(key=key)


@dataclasses.dataclass
class ParsedLessonInfo:
    paragraphs: list[LessonInfoParagraph]

    @classmethod
    def from_str(cls, info: str, lesson: models.Lesson, plan_type: typing.Literal["forms", "teachers", "rooms"]
                 ) -> ParsedLessonInfo:
        return cls([
            LessonInfoParagraph.from_str(paragraph.strip(), lesson, i, plan_type)
            for i, paragraph in enumerate(split_parens_aware(info, ";")) if paragraph.strip()
        ])

    def serialize(self, lesson_date: datetime.date, block_config: blocks.BlockConfiguration) -> list:
        return [paragraph.serialize(lesson_date, block_config) for paragraph in self.paragraphs]

    def sorted(self, key: typing.Callable[[LessonInfoParagraph], typing.Any]) -> ParsedLessonInfo:
        return ParsedLessonInfo(sorted(self.paragraphs, key=key))

    def sort(self, key: typing.Callable[[LessonInfoParagraph], typing.Any]):
        self.paragraphs.sort(key=key)

    def sort_original(self):
        self.sort(key=lambda p: p.index)

        for paragraph in self.paragraphs:
            paragraph.sort(key=lambda i: i.index)

    def sorted_canonical(self) -> ParsedLessonInfo:
        paragraphs = [p.sorted(key=lambda i: i.parsed.original_messages) for p in self.paragraphs]

        return ParsedLessonInfo(
            sorted(paragraphs, key=lambda p: [i.parsed.original_messages for i in p.messages])
        )

    def resolve_teachers(self, teachers: teacher_model.Teachers, date: datetime.date | None = None):
        for paragraph in self.paragraphs:
            for message in paragraph.messages:
                if hasattr(message.parsed, "_teachers"):
                    try:
                        message.parsed.other_info_value = {
                            teachers.query_plan_teacher(teacher_str, date=date).plan_short for teacher_str in
                            message.parsed._teachers
                        }
                    except LookupError:
                        message.parsed.other_info_value = None

    def lesson_group_sort_key(self) -> list[list[list[str]]]:
        return [
            [message.parsed.original_messages for message in paragraph.messages]
            for paragraph in self.sorted_canonical().paragraphs
        ]

    def filter_messages(self, func: typing.Callable[[LessonInfoMessage], bool]) -> ParsedLessonInfo:
        out = ParsedLessonInfo([])

        for paragraph in self.paragraphs:
            filtered_messages = LessonInfoParagraph(list(filter(func, paragraph.messages)), paragraph.index)
            if filtered_messages.messages:
                out.paragraphs.append(filtered_messages)

        return out

    def __add__(self, other: ParsedLessonInfo):
        if not isinstance(other, ParsedLessonInfo):
            return NotImplemented

        return ParsedLessonInfo(self.paragraphs + other.paragraphs)


def extract_teachers(lesson: models.Lesson, classes: dict[str, models.Class], *,
                     logger: logging.Logger) -> typing.Iterable[teacher_model.Teacher]:
    out: dict[str, teacher_model.Teacher] = {}

    for plan_short in lesson.teachers or ():
        out[plan_short] = teacher_model.Teacher(
            plan_short=plan_short,
            last_seen=lesson._lesson_date,
            first_seen=lesson._lesson_date
        )

    if lesson._is_scheduled:
        return ()

    for paragraph in lesson.parsed_info.paragraphs:
        for message in paragraph.messages:
            if (
                getattr(message.parsed, "other_info_value", -1) is None and
                hasattr(message.parsed, "course") and
                getattr(message.parsed, "_teachers", None) is not None
            ):
                # noinspection PyUnresolvedReferences
                surname = next(iter(message.parsed._teachers))
                course = message.parsed.course

                # TODO: Better discrimination criteria between teacher surname and abbreviation
                if len(surname.split()) == 1:
                    continue

                _class: dict[str, models.Class] = {
                    class_nr: class_ for class_nr, class_ in classes.items()
                    if (
                        course == (class_.group or class_.subject)
                        and lesson.forms.issubset(class_.forms)
                    )
                }

                if len(_class) == 0:
                    _class = {
                        class_nr: class_ for class_nr, class_ in classes.items()
                        if (
                            course == class_.subject
                            and lesson.forms.issubset(class_.forms)
                        )
                    }

                _name = surname.split()[1]
                if len({c.teacher for c in _class.values()}) > 1 and lesson.class_opt.number:
                    new_classes = {c_id: c for c_id, c in _class.items() if c_id == lesson.class_opt.number}
                    if new_classes:
                        _class = new_classes

                if len({c.teacher for c in _class.values()}) > 1:
                    _class = {c_id: c for c_id, c in _class.items() if c.teacher and _name.startswith(c.teacher[0])}

                if len({c.teacher for c in _class.values()}) != 1:
                    logger.debug(
                        f"Could not find class {course!r} in form {lesson.forms!r}. "
                        f"Message: {message.parsed.original_messages!r}"
                    )
                    continue

                abbreviation = list(_class.values())[0].teacher

                if not any(c.isalpha() for c in abbreviation):
                    logger.debug(f"Class {course!r} in form {lesson.forms!r} contains invalid teacher {abbreviation!r}")
                    continue

                out[abbreviation] = teacher_model.Teacher(
                    plan_short=abbreviation,
                    plan_long=surname,
                    last_seen=lesson._lesson_date,
                    first_seen=lesson._lesson_date,
                )

    return out.values()


def process_additional_info(info: list[str], parsed_existing_forms: list[ParsedForm],
                            teachers: teacher_model.Teachers, date: datetime.date, rooms: set[str]
                            ) -> list[list[LessonInfoTextSegment]]:
    info = [l or "" for l in info]

    return [
        process_additional_info_line(line, parsed_existing_forms, teachers, date, rooms=rooms)
        for line in info
    ]


def process_additional_info_line(text: str, parsed_existing_forms: list[ParsedForm],
                                 teachers: teacher_model.Teachers, date: datetime.date, rooms: set[str]
                                 ) -> list[LessonInfoTextSegment]:
    text = typography_fixer.fix_typography(text)

    # TODO: Dates, Rooms

    funcs = (
        lambda s: add_fuzzy_teacher_links(s, text, teachers, date),
        lambda s: add_fuzzy_form_links(s, text, parsed_existing_forms, date),
        lambda s: add_fuzzy_room_links(s, text, rooms, date)
    )

    segments = [LessonInfoTextSegment(text)]
    for func in funcs:
        new_segments = []
        for segment in segments:
            if segment.link is None:
                new_segments += func(segment.text)
            else:
                new_segments.append(segment)
        segments = group_text_segments(new_segments)

    return segments


def add_fuzzy_with_validator(
    text: str,
    patterns: typing.Iterable[re.Pattern],
    validator: typing.Callable[[re.Match], list[LessonInfoTextSegment] | None]
) -> list[LessonInfoTextSegment]:
    segments = []

    i = 0
    while i < len(text):
        matches = (pattern.match(text, i) for pattern in patterns)

        for match in matches:
            if match is None:
                continue

            new_segments = validator(match)

            if new_segments is None:
                continue

            segments += new_segments
            i += match.end()
            break
        else:
            segments.append(LessonInfoTextSegment(text[i]))
            i += 1

    return segments


def add_fuzzy_form_links(text: str, _, parsed_existing_forms: list[ParsedForm], date: datetime.date
                         ) -> list[LessonInfoTextSegment]:
    def validator(match: re.Match) -> list[LessonInfoTextSegment] | None:
        parsed_forms = ParsedForm.from_form_match(match)

        matched_forms: list[ParsedForm] = []

        for parsed_form in parsed_forms.expand_forms():
            form_match = None

            for existing_form in parsed_existing_forms:
                if parsed_form.isalpha() and existing_form.isalpha():
                    if (not existing_form[0].isnumeric()) and parsed_form[0] == existing_form[0]:
                        form_match = existing_form
                        break
                elif not parsed_form.isalpha() and not existing_form.isalpha():
                    try:
                        is_match = int(existing_form[0]) == int(parsed_form[0].lower())
                    except ValueError:
                        is_match = existing_form[0].lower() == parsed_form[0].lower()

                    if is_match and existing_form[2] == parsed_form[2]:
                        form_match = existing_form
                        break

            if form_match is not None:
                matched_forms.append(form_match)

        if matched_forms:
            return [
                LessonInfoTextSegment(
                    parsed_forms_to_str(matched_forms),
                    link=LessonInfoTextSegmentLink("forms", [f.to_str() for f in matched_forms], date, None)
                )
            ]
        else:
            return None

    return add_fuzzy_with_validator(text, [_loose_parse_form_pattern], validator)


def add_fuzzy_teacher_links(text: str, context: str, teachers: teacher_model.Teachers, date: datetime.date):
    def validator(match: re.Match) -> list[LessonInfoTextSegment] | None:
        # check if word can be teacher (by grammar rules)
        doc = nlp(context)  # analyze info line
        # find teacher in info
        for i in doc:
            if doc[i].text == text:
                # check part of speach of suspected teacher
                # has to be proper noun to be accepted
                if doc[i].pos_ != "PROPN":
                    return None

        surname_or_abbreviation = match.group()

        replacements = {
            "Hr": "Herr",
            "Hr.": "Herr",
            "Fr": "Frau",
            "Fr.": "Frau",
            "Herrn": "Herr"
        }

        _surname_or_abbreviation = surname_or_abbreviation
        for k, v in replacements.items():
            _surname_or_abbreviation = _surname_or_abbreviation.replace(k, v)

        try:
            plan_short = teachers.query_plan_teacher(_surname_or_abbreviation, date=date).plan_short
        except LookupError:
            return

        return [
            LessonInfoTextSegment(
                surname_or_abbreviation,
                link=LessonInfoTextSegmentLink("teachers", [plan_short], date, None)
            )
        ]

    return add_fuzzy_with_validator(text, [re.compile(_InfoParsers._teacher), re.compile(r"\b\w+\b")], validator)


def add_fuzzy_room_links(text: str, _, rooms: set[str], date: datetime.date):
    def validator(match: re.Match) -> list[LessonInfoTextSegment] | None:
        room = match.group()
        return [
            LessonInfoTextSegment(
                room,
                link=LessonInfoTextSegmentLink("rooms", [room], date, None)
            )
        ]

    return add_fuzzy_with_validator(text, list(re.compile(fr"(?<!\S){r}(?=\s|[)!?:])") for r in rooms), validator)


def group_text_segments(segments: list[LessonInfoTextSegment]) -> list[LessonInfoTextSegment]:
    out = []

    for segment in segments:
        if not segment.text:
            continue
        if out and out[-1].link == segment.link:
            out[-1].text += segment.text
        else:
            out.append(segment)

    return out
