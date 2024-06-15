from shared.cache import Cache
from backend.models import Plan, Lessons, PlanLesson
from pprint import pprint
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from matplotlib.ticker import FuncFormatter

from stundenplan24_py import indiware_mobil

from xml.etree import ElementTree
import datetime
from pathlib import Path
from collections import defaultdict
import json

def main():
    cache = Cache(Path(".cache/10000000"))
    
    out: dict[str, dict[str, dict[str, tuple[bool, bool]]]] = defaultdict(lambda: defaultdict(dict))

    for day in cache.get_days():
        print(f"{day!s}")

        revisions = cache.get_timestamps(day)

        xml_file = cache.get_plan_file(day, revisions[0], filename="PlanKl.xml", newest_before=True)

        indiware_plan = indiware_mobil.IndiwareMobilPlan.from_xml(ElementTree.fromstring(xml_file))

        plan = Plan.from_form_plan(indiware_plan)

        plan_lessons = plan.lessons.make_plan("teachers", plan_type="forms")

        """
        Was wollen wir einer Stunde zuordnen  .is_unplanned    .takes_place
        - findet außerplanmäßig statt         True             True
        - findet regulär statt                False            True
        - fällt aus                           False            False
        Mehrere lessons in einer period: Maximum der values wird genutzt
        {
            "Mar": {
                "2024-20-21": {
                    "1": (True, True)
                }
            }
        }
        """

        for teacher, lessons in plan_lessons.items():
            lessons_by_period = Lessons(lessons).group_by("periods")
            
            for period, period_lessons in lessons_by_period.items():
                period_lessons: list[PlanLesson]
                is_unplanned = max([period_lesson.is_unplanned for period_lesson in period_lessons])
                takes_place = max([period_lesson.takes_place for period_lesson in period_lessons])

                out[teacher][day.isoformat()][period] = is_unplanned, takes_place
    
    with open("testout.json", "w") as f:
        json.dump(out, f)
    


def get_position(date: datetime.date, period: int, start_date: datetime.date) -> tuple[float, float, float, float]:
    start_date -= datetime.timedelta(days=start_date.weekday())

    day_width_total = 1
    day_pad = 0.2
    day_width = day_width_total - day_pad
    period_width = day_width / 10

    week_height_total = 1
    week_pad = 0.5
    week_height = week_height_total - week_pad
    weeks_since_start_date = (date - start_date).days // 7

    x1 = (day_width + day_pad) * date.weekday() + (period-1) * period_width
    y1 = weeks_since_start_date * (week_height + week_pad)

    return x1 - day_width / 2, y1 - week_height / 2, period_width, week_height


def get_yticklabel(start_date: datetime.date, ytick: int) -> str:
    if ytick % 1 != 0:
        return ""
    else:
        start_date -= datetime.timedelta(days=start_date.weekday())
        week_monday = start_date + datetime.timedelta(days=ytick*7)

        return f"{week_monday:%d.%m.%Y} (KW {str(week_monday.isocalendar().week):0>2})"


def get_color(lesson_values: list[bool]) -> str:
    if lesson_values is None:
        return "orange"
    elif lesson_values == [True, True]:
        return 'darkgreen'
    elif lesson_values == [False, True]:
        return 'green'
    elif lesson_values == [False, False]:
        return 'red'
    else:
        return "Das darf nicht passieren..."


def plot():
    teacher = "Czi"
    with open("testout.json", "r") as f:
        out = json.load(f)
    data = out[teacher]
    bars = []
    start_date = datetime.date.fromisoformat(min(data.keys()))
    for date, date_data in data.items():
        for i in range(1, 11):
            bars.append((get_position(datetime.date.fromisoformat(date), i, start_date), get_color(date_data.get(str(i)))))
    
    fig, ax = plt.subplots()

    for bar in bars:
        bbox, color = bar
        x, y, width, height = bbox

        ax.fill_between(
            x=[x, x+width],
            y1=[y, y],
            y2=[y+height, y+height],
            color=color
        )

    ax.xaxis.set_label_position('top')
    ax.xaxis.tick_top()
    ax.set_xticks([0,1,2,3,4])
    ax.set_xticklabels(['Montag', 'Dienstag', 'Mittwoch', 'Donnerstag', 'Freitag'])

    formatter = FuncFormatter(lambda ytick, tick_pos: get_yticklabel(start_date, ytick))

    ax.yaxis.set_major_formatter(formatter)

    plt.gca().invert_yaxis()
    plt.title(f"Lehrer: {teacher}")
    plt.tight_layout()
    plt.savefig('test.png', dpi=300)
    


if __name__ == "__main__":
    plot()
