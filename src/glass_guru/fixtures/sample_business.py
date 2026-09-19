"""A deterministic sample glass business.

Used by unit tests, golden scenarios, and the local dev server seed. Everything is
fixed - no randomness, no ``now()`` - so any scenario built on it replays identically.

The shape mirrors the real business this project is modelled on: six workers whose
certifications genuinely do not overlap, four vans with different racks and stock,
and a job mix that forces two-person crews and material lead times to matter.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

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

#: Business timezone. Seattle, which is where the sample geography sits.
BUSINESS_TZ = timezone(timedelta(hours=-7))

#: Monday of the sample week. All scenarios are anchored to this.
WEEK_START = date(2026, 9, 21)

DEPOT = Location(lat=47.6205, lon=-122.3493, address="1200 Industrial Way, Seattle WA")

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
        home_location=Location(lat=47.6588, lon=-122.3120, address="Wallingford"),
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
        home_location=Location(lat=47.5480, lon=-122.3140, address="Georgetown"),
        loaded_cost_per_hour=60.0,
    ),
    Worker(
        id="w-dan",
        name="Dan",
        certifications=frozenset(
            {Certification.RESIDENTIAL_GLAZING, Certification.TEMPERED_SAFETY}
        ),
        working_hours=DAY_SHIFT,
        home_location=Location(lat=47.6820, lon=-122.2570, address="Laurelhurst"),
        loaded_cost_per_hour=55.0,
    ),
    Worker(
        id="w-sofia",
        name="Sofia",
        certifications=frozenset({Certification.AUTO_GLASS, Certification.TEMPERED_SAFETY}),
        working_hours=DAY_SHIFT,
        home_location=Location(lat=47.5301, lon=-122.2860, address="Rainier Beach"),
        loaded_cost_per_hour=57.0,
    ),
    Worker(
        id="w-ken",
        name="Ken",
        certifications=frozenset({Certification.RESIDENTIAL_GLAZING, Certification.SCREEN_REPAIR}),
        working_hours=DAY_SHIFT,
        home_location=Location(lat=47.7010, lon=-122.3430, address="Northgate"),
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
        home_location=Location(lat=47.6100, lon=-122.2000, address="Bellevue"),
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
        location=Location(lat=lat, lon=lon, address=address),
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
        lat=47.6145,
        lon=-122.3400,
        address="1520 2nd Ave, Seattle",
        service=ServiceType.STOREFRONT_GLASS,
        duration=150,
        certs={Certification.COMMERCIAL_STOREFRONT},
        crew=2,
        day=0,
        win=(6, 9),
        hardness=WindowHardness.HARD,
        priority=Priority.HIGH,
        revenue=2800.0,
        property_type=PropertyType.COMMERCIAL,
        notes="Must finish before doors open at 09:00. Alley access only.",
        glass=GlassSpec(pane_count=2, glass_type=GlassType.LAMINATED),
    ),
    _job(
        jid="j-402",
        name="Chen Residence",
        lat=47.6710,
        lon=-122.3870,
        address="4410 Ballard Ave NW",
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
        lat=47.6620,
        lon=-122.3130,
        address="1800 N 45th St",
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
        lat=47.5450,
        lon=-122.3000,
        address="6200 Airport Way S",
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
        lat=47.7050,
        lon=-122.3350,
        address="10200 Aurora Ave N",
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
        lat=47.6350,
        lon=-122.2900,
        address="2600 Madison St",
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
        lat=47.6010,
        lon=-122.3310,
        address="800 Pike St",
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
        lat=47.6890,
        lon=-122.2610,
        address="5500 Sand Point Way NE",
        service=ServiceType.RESIDENTIAL_WINDOW_REPLACEMENT,
        duration=100,
        certs={Certification.RESIDENTIAL_GLAZING},
        day=1,
        win=(9, 16),
        revenue=640.0,
        present=True,
    ),
    # Far south, low revenue: the job a pure cost objective will defer forever
    # unless the escalating unserved penalty stops it.
    _job(
        jid="j-409",
        name="Alvarez Screens (far)",
        lat=47.4400,
        lon=-122.2400,
        address="14800 Des Moines Memorial Dr",
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
        lat=47.5900,
        lon=-122.3100,
        address="2400 4th Ave S",
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
