"""Closed vocabularies for the dispatch domain.

These are deliberately small and explicit. Agents select from these values rather
than inventing free-form strings, which is what makes their output checkable.
"""

from enum import StrEnum


class Certification(StrEnum):
    """Skills that gate which worker may be assigned to a job."""

    RESIDENTIAL_GLAZING = "residential_glazing"
    COMMERCIAL_STOREFRONT = "commercial_storefront"
    AUTO_GLASS = "auto_glass"
    TEMPERED_SAFETY = "tempered_safety"
    SCREEN_REPAIR = "screen_repair"
    SHOWER_DOOR = "shower_door"


class ServiceType(StrEnum):
    """What the crew is actually going out to do."""

    RESIDENTIAL_WINDOW_REPLACEMENT = "residential_window_replacement"
    STOREFRONT_GLASS = "storefront_glass"
    AUTO_GLASS = "auto_glass"
    EMERGENCY_BOARD_UP = "emergency_board_up"
    SCREEN_REPAIR = "screen_repair"
    SHOWER_DOOR_INSTALL = "shower_door_install"
    MEASURE_QUOTE = "measure_quote"


class GlassType(StrEnum):
    ANNEALED = "annealed"
    TEMPERED = "tempered"
    LAMINATED = "laminated"
    INSULATED_UNIT = "insulated_unit"


class PropertyType(StrEnum):
    RESIDENTIAL = "residential"
    COMMERCIAL = "commercial"
    VEHICLE = "vehicle"


class WindowHardness(StrEnum):
    """Whether a customer time window may be violated at a price, or not at all.

    HARD means infeasible outside the window - a storefront that can only be worked
    on before it opens. SOFT means the solver may run late for a per-minute penalty.
    """

    HARD = "hard"
    SOFT = "soft"


class Priority(StrEnum):
    EMERGENCY = "emergency"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


class CommitmentState(StrEnum):
    """How firmly a job is attached to the plan. Drives what may move it.

    See the living-plan design: DRAFT and PROVISIONAL are freely movable by the
    cold path, CONFIRMED requires human approval because a customer was given a
    window, and DISPATCHED onward is locked to repair mode.
    """

    DRAFT = "draft"
    PROVISIONAL = "provisional"
    CONFIRMED = "confirmed"
    DISPATCHED = "dispatched"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


#: States whose scheduled time the cold-path reoptimizer may change freely.
MOVABLE_STATES: frozenset[CommitmentState] = frozenset(
    {CommitmentState.DRAFT, CommitmentState.PROVISIONAL}
)

#: States that pin a job to its current crew and time; only repair mode touches these.
LOCKED_STATES: frozenset[CommitmentState] = frozenset(
    {CommitmentState.DISPATCHED, CommitmentState.COMPLETED}
)

#: States that no longer consume capacity.
TERMINAL_STATES: frozenset[CommitmentState] = frozenset(
    {CommitmentState.COMPLETED, CommitmentState.CANCELLED}
)


class UnservedReason(StrEnum):
    """Machine-derived explanation for why the solver could not place a job.

    Surfaced verbatim to the dispatcher, so every value must be actionable.
    """

    NO_CERTIFIED_WORKER = "no_certified_worker"
    NO_CAPACITY_IN_HORIZON = "no_capacity_in_horizon"
    WINDOW_ON_ANOTHER_DAY = "window_on_another_day"
    HARD_WINDOW_UNREACHABLE = "hard_window_unreachable"
    MATERIALS_NOT_AVAILABLE = "materials_not_available"
    CREW_SIZE_UNAVAILABLE = "crew_size_unavailable"
    VAN_CAPACITY_EXCEEDED = "van_capacity_exceeded"
    OUTSIDE_SERVICE_RADIUS = "outside_service_radius"
    COST_EXCEEDS_VALUE = "cost_exceeds_value"


class ViolationCode(StrEnum):
    """Invariant breaches. Any of these blocks a plan from being committed."""

    WORKER_DOUBLE_BOOKED = "worker_double_booked"
    VAN_DOUBLE_BOOKED = "van_double_booked"
    WORKER_UNAVAILABLE = "worker_unavailable"
    VAN_UNAVAILABLE = "van_unavailable"
    HARD_WINDOW_VIOLATED = "hard_window_violated"
    MISSING_CERTIFICATION = "missing_certification"
    CREW_SIZE_MISMATCH = "crew_size_mismatch"
    VAN_CAPACITY_EXCEEDED = "van_capacity_exceeded"
    TRAVEL_TIME_INCONSISTENT = "travel_time_inconsistent"
    OUTSIDE_WORKING_HOURS = "outside_working_hours"
    MATERIALS_UNAVAILABLE = "materials_unavailable"
    CONFIRMED_WINDOW_MOVED = "confirmed_window_moved"
    LOCKED_JOB_MOVED = "locked_job_moved"
    DUPLICATE_JOB_ASSIGNMENT = "duplicate_job_assignment"
    UNKNOWN_ENTITY_REFERENCE = "unknown_entity_reference"
    SCHEDULED_BEFORE_REQUEST = "scheduled_before_request"
