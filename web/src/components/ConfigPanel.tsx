import { useEffect, useState } from "react";

import { api } from "../api";
import type { World, Worker } from "../types";

/**
 * The roster, editable.
 *
 * Today a person types this in; the shape of each row is deliberately the subset a
 * payroll or HR export would carry (name, phone, certs, shift, overtime), so that
 * when such a system is connected one day it feeds the same endpoint this form
 * does, and the form becomes a viewer with an override.
 */

// The three skills work is actually gated on. No auto glass (we fit buildings,
// never vehicles), no screen repair (needs hands, not a badge), no "tempered
// safety" (gated nothing - a phantom). The enum keeps the old values for old
// records; the product stops offering the words.
const ALL_CERTS = ["residential_glazing", "commercial_storefront", "shower_door"];

/** "residential glazing", not "rg". The first version squeezed six checkboxes with
 * two-letter codes into one table column, which overflowed the sheet and meant
 * nothing to anyone; the certifications get a wrapping line of named pills now. */
const certLabel = (cert: string) => cert.replace(/_/g, " ");

interface Row {
  id: string;
  name: string;
  phone: string;
  certifications: string[];
  shift_start: string;
  shift_end: string;
  overtime_eligible: boolean;
}

function rowFrom(worker: Worker): Row {
  return {
    id: worker.id,
    name: worker.name,
    phone: worker.phone,
    certifications: [...worker.certifications],
    shift_start: worker.shift_start || "08:00",
    shift_end: worker.shift_end || "17:00",
    overtime_eligible: worker.overtime_eligible,
  };
}

const FRESH: Row = {
  id: "",
  name: "",
  phone: "",
  certifications: [],
  shift_start: "08:00",
  shift_end: "17:00",
  overtime_eligible: true,
};

interface RateField {
  key: string;
  value: number;
  source: string;
  min: number;
  max: number;
}

/** What a van can carry, in the words the business uses. The fleet trim that
 * retired van-3 quietly took the only shower kits with it, and the board had no
 * way to even see that - "no van stocks 1x shower_kit" with nothing to click.
 * (auto_windshield is dead stock from the fixture; we fit buildings, not cars.) */
const PART_LABELS: Record<string, string> = {
  annealed_std: "annealed glass",
  tempered_std: "tempered glass",
  screen_kit: "screen kits",
  shower_kit: "shower kits",
  board_up_kit: "board-up kits",
};

const RATE_LABELS: Record<string, string> = {
  labour_rate_per_hour: "labour, per fitter-hour",
  call_out_fee: "call-out fee",
  materials_markup: "materials markup (×)",
  minimum_charge: "minimum charge",
  after_hours_rate_multiplier: "after-hours labour (×)",
  emergency_uplift: "emergency uplift (×)",
  tax_rate: "sales tax (fraction)",
};

export function ConfigPanel({
  world,
  owner,
  onClose,
  onChanged,
  onUnlock,
}: {
  world: World;
  owner: boolean;
  onClose: () => void;
  onChanged: () => void;
  /** Try a PIN; resolves true when the server agrees. Lives here as well as the
   * top bar because "close the sheet, find the Owner button, type, reopen the
   * sheet" was three errands for one unlock. */
  onUnlock: (pin: string) => Promise<boolean>;
}) {
  const [rows, setRows] = useState<Row[]>([...world.workers.map(rowFrom)]);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [movingShop, setMovingShop] = useState(false);
  const [rates, setRates] = useState<RateField[] | null>(null);
  const [rateDrafts, setRateDrafts] = useState<Record<string, string>>({});
  const [unlockPin, setUnlockPin] = useState("");
  const [unlockFailed, setUnlockFailed] = useState(false);

  async function tryUnlock() {
    setUnlockFailed(false);
    const ok = await onUnlock(unlockPin);
    if (!ok) setUnlockFailed(true);
    else setUnlockPin("");
  }

  useEffect(() => {
    if (!owner) return;
    void api
      .pricing()
      .then((r) => {
        setRates(r.fields);
        setRateDrafts(
          Object.fromEntries(r.fields.map((f) => [f.key, String(f.value)])),
        );
      })
      .catch(() => setRates(null));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [owner]);
  const [newDepot, setNewDepot] = useState("");
  // Per-van stock drafts, keyed van id -> part -> typed text. Seeded from the
  // world so the numbers on screen are the numbers the solver sees.
  const [stockDrafts, setStockDrafts] = useState<Record<string, Record<string, string>>>(
    () => Object.fromEntries(
      world.vans.map((v) => [
        v.id,
        Object.fromEntries(
          Object.keys(PART_LABELS).map((part) => [part, String(v.stock[part] ?? 0)]),
        ),
      ]),
    ),
  );

  function editStock(vanId: string, part: string, value: string) {
    // A van added after the sheet opened has no draft yet; seed it from the live
    // van so editing one part cannot silently zero the others on save.
    const van = world.vans.find((v) => v.id === vanId);
    const seeded = Object.fromEntries(
      Object.keys(PART_LABELS).map((k) => [k, String(van?.stock[k] ?? 0)]),
    );
    setStockDrafts((old) => ({
      ...old,
      [vanId]: { ...seeded, ...(old[vanId] ?? {}), [part]: value },
    }));
  }

  function stockDirty(vanId: string): boolean {
    const draft = stockDrafts[vanId];
    if (!draft) return false;
    const van = world.vans.find((v) => v.id === vanId);
    if (!van) return false;
    return Object.keys(PART_LABELS).some(
      (part) => (parseInt(draft[part] ?? "", 10) || 0) !== (van.stock[part] ?? 0),
    );
  }

  async function saveStock(vanId: string) {
    const draft = stockDrafts[vanId] ?? {};
    const stock = Object.fromEntries(
      Object.keys(PART_LABELS).map((part) => [
        part,
        Math.max(0, parseInt(draft[part] ?? "", 10) || 0),
      ]),
    );
    await run(
      () => api.configVan({ id: vanId, stock }),
      `${vanId} restocked`,
    );
  }

  const [confirmDepot, setConfirmDepot] = useState("");

  function edit(index: number, patch: Partial<Row>) {
    setRows((old) => old.map((r, i) => (i === index ? { ...r, ...patch } : r)));
  }

  async function run(work: () => Promise<unknown>, done: string) {
    setBusy(true);
    setStatus("");
    try {
      await work();
      setStatus(done);
      onChanged();
    } catch (exc) {
      setStatus((exc as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      className="config"
      onMouseDown={(e) => {
        // The dimmed backdrop is a door, not a wall: clicking it closes the sheet.
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="config__sheet">
        <header className="config__head">
          <h2>Crew and vans</h2>
          <p className="panel__hint">
            Edits land as roster events and the schedule re-plans around them. One
            day this pulls from payroll; until then, this is payroll.
          </p>
          <button className="config__close" onClick={onClose}>
            Close
          </button>
        </header>

        <div className="config__rows">
          {rows.map((row, index) => (
            <div className="cfgrow" key={row.id || `new-${index}`}>
              <div className="cfgrow__main">
                <input
                  className="cfgrow__name"
                  value={row.name}
                  placeholder="name"
                  onChange={(e) => edit(index, { name: e.target.value })}
                />
                <input
                  className="cfgrow__phone"
                  value={row.phone}
                  placeholder="+1 817…"
                  onChange={(e) => edit(index, { phone: e.target.value })}
                />
                <span className="cfgrow__shift">
                  <input
                    type="time"
                    value={row.shift_start}
                    onChange={(e) => edit(index, { shift_start: e.target.value })}
                  />
                  –
                  <input
                    type="time"
                    value={row.shift_end}
                    onChange={(e) => edit(index, { shift_end: e.target.value })}
                  />
                </span>
                <label className="cfgrow__ot" title="may work past shift on overtime">
                  <input
                    type="checkbox"
                    checked={row.overtime_eligible}
                    onChange={(e) => edit(index, { overtime_eligible: e.target.checked })}
                  />
                  overtime
                </label>
                <span className="cfgrow__actions">
                  <button
                    disabled={busy || !row.name.trim()}
                    onClick={() =>
                      void run(() => api.configWorker({ ...row }), `${row.name} saved`)
                    }
                  >
                    Save
                  </button>
                  {row.id && (
                    <button
                      className="danger"
                      disabled={busy}
                      onClick={() =>
                        void run(() => api.removeWorker(row.id), `${row.name} removed`).then(
                          () => setRows((old) => old.filter((_, i) => i !== index)),
                        )
                      }
                    >
                      Remove
                    </button>
                  )}
                </span>
              </div>
              <div className="cfgrow__certs">
                {ALL_CERTS.map((cert) => {
                  const on = row.certifications.includes(cert);
                  return (
                    <button
                      key={cert}
                      type="button"
                      className={`certpill${on ? " certpill--on" : ""}`}
                      onClick={() =>
                        edit(index, {
                          certifications: on
                            ? row.certifications.filter((c) => c !== cert)
                            : [...row.certifications, cert],
                        })
                      }
                    >
                      {certLabel(cert)}
                    </button>
                  );
                })}
              </div>
            </div>
          ))}
        </div>
        <button
          className="config__add"
          disabled={busy}
          onClick={() => setRows((old) => [...old, { ...FRESH }])}
        >
          + Add a fitter
        </button>

        <h3 className="config__vanshead">Vans and what they carry</h3>
        <p className="config__hint">
          A job is only offered when some van carries its parts - zero shower kits
          across the fleet means every shower call is refused until a van is
          restocked here.
        </p>
        <div className="config__vanlist">
          {world.vans.map((van) => (
            <div className="config__vanrow" key={van.id}>
              <span className="config__vanid">{van.id}</span>
              <span className="config__stock">
                {Object.entries(PART_LABELS).map(([part, label]) => (
                  <label className="config__part" key={part}>
                    <input
                      type="number"
                      min={0}
                      max={99}
                      value={stockDrafts[van.id]?.[part] ?? String(van.stock[part] ?? 0)}
                      disabled={busy}
                      onChange={(e) => editStock(van.id, part, e.target.value)}
                    />
                    {label}
                  </label>
                ))}
              </span>
              <button
                disabled={busy || !stockDirty(van.id)}
                onClick={() => void saveStock(van.id)}
              >
                Save stock
              </button>
              <button
                className="danger"
                disabled={busy || world.vans.length <= 1}
                title={world.vans.length <= 1 ? "the last van stays" : `retire ${van.id}`}
                onClick={() => void run(() => api.removeVan(van.id), `${van.id} removed`)}
              >
                Remove
              </button>
            </div>
          ))}
          <button
            disabled={busy}
            onClick={() => void run(() => api.configVan({ id: "", label: "" }), "van added")}
          >
            + Add a van
          </button>
        </div>

        {!owner && (
          <>
            <h3 className="config__vanshead">Owner controls</h3>
            <p className="muted">Prices and moving the shop are locked.</p>
            <span className="config__unlock">
              <input
                type="password"
                placeholder="owner PIN"
                value={unlockPin}
                onChange={(e) => setUnlockPin(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") void tryUnlock();
                }}
              />
              <button disabled={busy || !unlockPin} onClick={() => void tryUnlock()}>
                Unlock
              </button>
              {unlockFailed && <em className="warn">wrong PIN</em>}
            </span>
          </>
        )}
        {owner && rates && (
          <>
            <h3 className="config__vanshead">Prices (owner only)</h3>
            <p className="panel__hint">
              What every quote is built from. "estimated" means the shipped
              placeholder is still in force - each number you set here becomes the
              real price from the next call, and the "estimated costs" warning
              retires itself as you go.
            </p>
            <div className="config__rates">
              {rates.map((f) => (
                <label key={f.key} className="config__rate">
                  <span className="config__ratelabel">
                    {RATE_LABELS[f.key] ?? f.key}
                    <em className={f.source === "estimated" ? "warn" : "ok"}>
                      {f.source === "estimated" ? "estimated" : "set by you"}
                    </em>
                  </span>
                  <input
                    inputMode="decimal"
                    value={rateDrafts[f.key] ?? ""}
                    onChange={(e) =>
                      setRateDrafts((d) => ({ ...d, [f.key]: e.target.value }))
                    }
                  />
                </label>
              ))}
            </div>
            <button
              disabled={busy}
              onClick={() =>
                void run(async () => {
                  const entries: Record<string, number> = {};
                  for (const f of rates) {
                    const typed = Number(rateDrafts[f.key]);
                    if (Number.isFinite(typed) && typed !== f.value) entries[f.key] = typed;
                  }
                  if (Object.keys(entries).length === 0) return "nothing changed";
                  await api.setPricing(entries);
                  const fresh = await api.pricing();
                  setRates(fresh.fields);
                  return null;
                }, "prices set - quotes use them from the next call")
              }
            >
              Save prices
            </button>
          </>
        )}

        <h3 className="config__vanshead">The shop</h3>
        <div className="config__depot">
          {/* The single most consequential coordinate in the system - it was once
              wrong by four road miles and every route carried the error. So this
              is deliberately the hardest edit on the sheet: the new address must
              be TYPED twice, and the server then refuses anything that is not an
              actual building inside the service area. */}
          <p className="config__depotaddr">
            Every route starts and ends at <strong>{world.depot_address || "(no depot)"}</strong>
          </p>
          {!owner ? (
            <p className="muted">moving the shop needs the owner's PIN</p>
          ) : !movingShop ? (
            <button disabled={busy} onClick={() => setMovingShop(true)}>
              Move the shop…
            </button>
          ) : (
            <div className="config__depotform">
              <p className="config__depotwarn">
                Moving the shop changes every drive on every future plan. Type the
                new address twice - no pasting the second one.
              </p>
              <input
                value={newDepot}
                placeholder="new address, with street number"
                onChange={(e) => setNewDepot(e.target.value)}
              />
              <input
                value={confirmDepot}
                placeholder="retype it to confirm"
                onPaste={(e) => e.preventDefault()}
                onChange={(e) => setConfirmDepot(e.target.value)}
              />
              <span className="config__depotactions">
                <button
                  className="danger"
                  disabled={
                    busy || !newDepot.trim() || newDepot.trim() !== confirmDepot.trim()
                  }
                  title={
                    newDepot.trim() === confirmDepot.trim()
                      ? "move every van's home to this address"
                      : "the two entries must match exactly"
                  }
                  onClick={() =>
                    void run(async () => {
                      const moved = await api.moveDepot(newDepot.trim(), confirmDepot.trim());
                      setMovingShop(false);
                      setNewDepot("");
                      setConfirmDepot("");
                      return moved;
                    }, "the shop moved - every van now starts from the new address")
                  }
                >
                  Move it
                </button>
                <button
                  disabled={busy}
                  onClick={() => {
                    setMovingShop(false);
                    setNewDepot("");
                    setConfirmDepot("");
                  }}
                >
                  Never mind
                </button>
              </span>
            </div>
          )}
        </div>

        {status && <p className="config__status">{status}</p>}
      </div>
    </div>
  );
}
