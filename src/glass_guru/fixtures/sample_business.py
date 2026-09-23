"""A deterministic sample glass business.

Used by unit tests, golden scenarios, and the local dev server seed. Everything is
fixed - no randomness, no ``now()`` - so any scenario built on it replays identically.

The shape mirrors the real business this project is modelled on: six workers whose
certifications genuinely do not overlap, four vans with different racks and stock,
and a job mix that forces two-person crews and material lead times to matter.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from glass_guru.domain.enums import (
    Certification,
    GlassType,
    Priority,
    PropertyType,
    ServiceType,
    WindowHardness,
)
from glass_guru.domain.events import Event, JobRequested, VanRegistered, WorkerRegistered
from glass_guru.domain.models import (
    DayHours,
    GlassSpec,
    Job,
    Location,
    Material,
    TimeWindow,
    Van,
    Worker,
)

#: Business timezone. Haslet, Texas, north of Fort Worth, which is where the
#: sample geography sits.

#: Real coordinates, resolved by ``scripts/geocode_fixture.py`` and committed so the
#: fixture is reproducible without network access. The literals passed to :func:`_geo`
#: below are the original hand-written guesses, kept only as a fallback and as a record
#: of how far off they were - median drift was about a mile, and the depot had been
#: placed at the Space Needle.
_GEOCODE_PATH = Path(__file__).resolve().parents[3] / "config" / "geocode_cache.json"
try:
    _GEOCODED: dict[str, dict[str, float]] = json.loads(_GEOCODE_PATH.read_text())
except OSError:  # pragma: no cover - only when the repo layout is unavailable
    _GEOCODED = {}


def _geo(key: str, lat: float, lon: float, address: str) -> Location:
    """Prefer the geocoded coordinate; fall back to the hand-written one."""
    entry = _GEOCODED.get(key)
    if entry is None:
        return Location(lat=lat, lon=lon, address=address)
    return Location(lat=float(entry["lat"]), lon=float(entry["lon"]), address=address)


#: Central Daylight Time. The business is in Haslet, north of Fort Worth.
BUSINESS_TZ = timezone(timedelta(hours=-5))

#: Monday of the sample week. All scenarios are anchored to this.
WEEK_START = date(2026, 9, 21)

DEPOT = _geo("depot", 33.0019853, -97.3423728, "150 Blue Mound Rd W #807, Haslet, TX 76052")

#: The downtown storefront, exported so the API's readiness probe can ask for a leg
#: the committed snapshot actually holds. Keyed the same way the job is, so it moves
#: with the geocode cache rather than drifting away from it.
PROBE_STOP = _geo("j-401", 32.7549, -97.3315, "420 Main St, Fort Worth, TX")

#: Standard shift for most of the roster.
DAY_SHIFT = tuple(DayHours(weekday=d, start=time(8, 0), end=time(17, 0)) for d in range(5))

#: Early shift. Storefront glass has to be finished before a business opens, so the
#: commercial-certified workers start at 06:00. Without this the hard pre-opening
#: windows are unreachable and every storefront job is correctly reported unserved.
EARLY_SHIFT = tuple(DayHours(weekday=d, start=time(6, 0), end=time(15, 0)) for d in range(5))


def _at(day_offset: int, hour: int, minute: int = 0) -> datetime:
    """A timezone-aware moment relative to the sample week's Monday."""
    d = WEEK_START + timedelta(days=day_offset)
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=BUSINESS_TZ)


# --------------------------------------------------------------------------- roster

WORKERS: tuple[Worker, ...] = (
    Worker(
        id="w-marcus",
        name="Marcus",
        # The only commercial-storefront certification on the roster. Deliberate:
        # it makes storefront jobs a genuine bottleneck the coordinator must reason about.
        certifications=frozenset(
            {
                Certification.COMMERCIAL_STOREFRONT,
                Certification.RESIDENTIAL_GLAZING,
                Certification.TEMPERED_SAFETY,
            }
        ),
        working_hours=EARLY_SHIFT,
        home_location=_geo("w-marcus", 32.9409, -97.1302, "Southlake, TX"),
        loaded_cost_per_hour=62.0,
    ),
    Worker(
        id="w-priya",
        name="Priya",
        certifications=frozenset(
            {
                Certification.COMMERCIAL_STOREFRONT,
                Certification.RESIDENTIAL_GLAZING,
                Certification.SHOWER_DOOR,
            }
        ),
        working_hours=EARLY_SHIFT,
        home_location=_geo("w-priya", 32.7696, -97.3086, "2600 E Belknap St, Fort Worth, TX"),
        loaded_cost_per_hour=60.0,
    ),
    Worker(
        id="w-dan",
        name="Dan",
        certifications=frozenset(
            {Certification.RESIDENTIAL_GLAZING, Certification.TEMPERED_SAFETY}
        ),
        working_hours=DAY_SHIFT,
        home_location=_geo("w-dan", 33.1866, -97.108, "Denton, TX"),
        loaded_cost_per_hour=55.0,
    ),
    Worker(
        id="w-sofia",
        name="Sofia",
        certifications=frozenset({Certification.AUTO_GLASS, Certification.TEMPERED_SAFETY}),
        working_hours=DAY_SHIFT,
        home_location=_geo("w-sofia", 32.8091, -97.2, "Hurst, TX"),
        loaded_cost_per_hour=57.0,
    ),
    Worker(
        id="w-ken",
        name="Ken",
        certifications=frozenset({Certification.RESIDENTIAL_GLAZING, Certification.SCREEN_REPAIR}),
        working_hours=DAY_SHIFT,
        home_location=_geo("w-ken", 32.9678, -97.2902, "Alliance, Fort Worth, TX"),
        loaded_cost_per_hour=48.0,
        overtime_eligible=False,
    ),
    Worker(
        id="w-alex",
        name="Alex",
        certifications=frozenset(
            {Certification.SCREEN_REPAIR, Certification.SHOWER_DOOR, Certification.AUTO_GLASS}
        ),
        working_hours=DAY_SHIFT,
        home_location=_geo("w-alex", 33.0431, -97.0165, "Lewisville, TX"),
        loaded_cost_per_hour=52.0,
    ),
)

VANS: tuple[Van, ...] = (
    Van(
        id="van-1",
        label="Van 1 - large rack",
        rack_slots=12,
        stock={"annealed_std": 6, "tempered_std": 4, "screen_kit": 3, "board_up_kit": 2},
        home_depot=DEPOT,
    ),
    Van(
        id="van-2",
        label="Van 2 - standard",
        rack_slots=8,
        stock={"annealed_std": 4, "screen_kit": 5, "board_up_kit": 1},
        home_depot=DEPOT,
    ),
    Van(
        id="van-3",
        label="Van 3 - standard",
        rack_slots=8,
        stock={"annealed_std": 4, "tempered_std": 2, "shower_kit": 2},
        home_depot=DEPOT,
    ),
    Van(
        id="van-4",
        label="Van 4 - auto glass",
        rack_slots=6,
        stock={"auto_windshield": 3, "auto_side": 4, "board_up_kit": 1},
        home_depot=DEPOT,
    ),
)


# ----------------------------------------------------------------------------- jobs


def _job(
    *,
    jid: str,
    name: str,
    lat: float,
    lon: float,
    address: str,
    service: ServiceType,
    duration: int,
    certs: set[Certification],
    crew: int = 1,
    day: int = 0,
    win: tuple[int, int] = (8, 17),
    hardness: WindowHardness = WindowHardness.SOFT,
    priority: Priority = Priority.NORMAL,
    revenue: float = 450.0,
    materials: tuple[Material, ...] = (),
    property_type: PropertyType = PropertyType.RESIDENTIAL,
    notes: str = "",
    glass: GlassSpec | None = None,
    present: bool = False,
    requested_offset: int = -4,
    requested_hour: int = 9,
) -> Job:
    return Job(
        id=jid,
        customer_id=f"c-{jid}",
        customer_name=name,
        phone="206-555-0100",
        location=_geo(jid, lat, lon, address),
        property_type=property_type,
        service_type=service,
        required_certifications=frozenset(certs),
        crew_size=crew,
        estimated_duration_min=duration,
        windows=(TimeWindow(start=_at(day, win[0]), end=_at(day, win[1]), hardness=hardness),),
        priority=priority,
        revenue=revenue,
        materials=materials,
        requires_customer_present=present,
        site_notes=notes,
        glass_spec=glass,
        requested_at=_at(requested_offset, requested_hour),
    )


JOBS: tuple[Job, ...] = (
    # A hard-window storefront needing the scarce commercial cert and two people.
    # This is the job that should be hard to place, and loudly explained when it isn't.
    _job(
        jid="j-401",
        name="Rodriguez Storefront",
        lat=32.9034,
        lon=-97.2573,
        address="1200 S Main St, Keller, TX",
        service=ServiceType.STOREFRONT_GLASS,
        duration=150,
        certs={Certification.COMMERCIAL_STOREFRONT},
        crew=2,
        day=0,
        win=(6, 10),
        hardness=WindowHardness.HARD,
        priority=Priority.HIGH,
        revenue=2800.0,
        property_type=PropertyType.COMMERCIAL,
        notes="Must finish before doors open at 10:00. Alley access only.",
        glass=GlassSpec(pane_count=2, glass_type=GlassType.LAMINATED),
    ),
    _job(
        jid="j-402",
        name="Chen Residence",
        lat=32.8984,
        lon=-97.3283,
        address="2201 N Tarrant Pkwy, Fort Worth, TX",
        service=ServiceType.RESIDENTIAL_WINDOW_REPLACEMENT,
        duration=120,
        certs={Certification.RESIDENTIAL_GLAZING},
        day=0,
        win=(9, 15),
        revenue=780.0,
        present=True,
        notes="Gate code 4412. Parking is brutal, allow 10 min.",
    ),
    _job(
        jid="j-403",
        name="Patel Shower Door",
        lat=32.7549,
        lon=-97.3315,
        address="420 Main St, Fort Worth, TX",
        service=ServiceType.SHOWER_DOOR_INSTALL,
        duration=150,
        certs={Certification.SHOWER_DOOR},
        day=0,
        win=(10, 16),
        revenue=1100.0,
        materials=(Material(part_code="shower_kit", quantity=1, in_stock=True),),
    ),
    _job(
        jid="j-404",
        name="Okonkwo Auto Glass",
        lat=32.8474,
        lon=-97.3602,
        address="101 N Main St, Saginaw, TX",
        service=ServiceType.AUTO_GLASS,
        duration=90,
        certs={Certification.AUTO_GLASS},
        day=0,
        win=(8, 17),
        revenue=520.0,
        property_type=PropertyType.VEHICLE,
        materials=(Material(part_code="auto_windshield", quantity=1, in_stock=True),),
    ),
    _job(
        jid="j-405",
        name="Nakamura Screens",
        lat=33.0125,
        lon=-97.2356,
        address="300 W Byron Nelson Blvd, Roanoke, TX",
        service=ServiceType.SCREEN_REPAIR,
        duration=45,
        certs={Certification.SCREEN_REPAIR},
        day=0,
        win=(8, 17),
        revenue=180.0,
        materials=(Material(part_code="screen_kit", quantity=2, in_stock=True),),
    ),
    # Tempered unit on a three-day order: cannot be installed before Thursday no
    # matter how good the routing is. Exercises the material-lead-time constraint.
    _job(
        jid="j-406",
        name="Whitfield Tempered",
        lat=33.0071,
        lon=-97.2023,
        address="5100 Trophy Club Dr, Trophy Club, TX",
        service=ServiceType.RESIDENTIAL_WINDOW_REPLACEMENT,
        duration=180,
        certs={Certification.TEMPERED_SAFETY, Certification.RESIDENTIAL_GLAZING},
        day=3,
        win=(8, 17),
        revenue=1450.0,
        materials=(
            Material(part_code="tempered_custom", quantity=1, in_stock=False, lead_time_days=3),
        ),
        glass=GlassSpec(pane_count=1, glass_type=GlassType.TEMPERED),
        requested_offset=0,
        requested_hour=7,
    ),
    _job(
        jid="j-407",
        name="Delgado Storefront",
        lat=32.9666,
        lon=-97.0435,
        address="3000 Grapevine Mills Pkwy, Grapevine, TX",
        service=ServiceType.STOREFRONT_GLASS,
        duration=180,
        certs={Certification.COMMERCIAL_STOREFRONT},
        crew=2,
        day=1,
        win=(7, 11),
        hardness=WindowHardness.HARD,
        revenue=2200.0,
        property_type=PropertyType.COMMERCIAL,
    ),
    _job(
        jid="j-408",
        name="Brennan Residence",
        lat=32.8776,
        lon=-97.2615,
        address="8851 Denton Hwy, Watauga, TX",
        service=ServiceType.RESIDENTIAL_WINDOW_REPLACEMENT,
        duration=100,
        certs={Certification.RESIDENTIAL_GLAZING},
        day=1,
        win=(9, 16),
        revenue=640.0,
        present=True,
    ),
    # Low revenue, out in Burien: together with Nakamura up north, the kind of job a
    # pure cost objective defers forever unless the escalating unserved penalty
    # stops it. (Which of the two is furthest depends on the depot, so neither is
    # labelled 'far' - the geocoded depot moved south and swapped them.)
    _job(
        jid="j-409",
        name="Alvarez Screens",
        lat=32.8549,
        lon=-97.1871,
        address="6401 Grapevine Hwy, North Richland Hills, TX",
        service=ServiceType.SCREEN_REPAIR,
        duration=45,
        certs={Certification.SCREEN_REPAIR},
        day=2,
        win=(8, 17),
        revenue=150.0,
        materials=(Material(part_code="screen_kit", quantity=1, in_stock=True),),
    ),
    _job(
        jid="j-410",
        name="Tran Auto Glass",
        lat=32.7474,
        lon=-97.0632,
        address="501 N Watson Rd, Arlington, TX",
        service=ServiceType.AUTO_GLASS,
        duration=75,
        certs={Certification.AUTO_GLASS},
        day=2,
        win=(8, 17),
        revenue=480.0,
        property_type=PropertyType.VEHICLE,
        materials=(Material(part_code="auto_side", quantity=1, in_stock=True),),
    ),
)


def seed_events(dispatch_id: str = "seed") -> list[Event]:
    """Registration and job-request events that build the sample world.

    Ordered so ``fold`` produces a world with a full roster and ten provisional jobs.
    """
    t = _at(0, 6)
    events: list[Event] = []
    for i, worker in enumerate(WORKERS):
        events.append(
            WorkerRegistered(
                event_id=f"seed-w-{i}",
                occurred_at=t,
                recorded_at=t,
                dispatch_id=dispatch_id,
                worker=worker,
            )
        )
    for i, van in enumerate(VANS):
        events.append(
            VanRegistered(
                event_id=f"seed-v-{i}",
                occurred_at=t,
                recorded_at=t,
                dispatch_id=dispatch_id,
                van=van,
            )
        )
    for i, job in enumerate(JOBS):
        events.append(
            JobRequested(
                event_id=f"seed-j-{i}",
                occurred_at=job.requested_at,
                recorded_at=job.requested_at,
                dispatch_id=dispatch_id,
                job=job,
            )
        )
    return events


def sample_world_at(day_offset: int = 0, hour: int = 8) -> tuple[list[Event], datetime]:
    """Seed events plus the moment to fold them at. Convenience for tests and demos."""
    return seed_events(), _at(day_offset, hour)
