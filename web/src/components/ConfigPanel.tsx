import { useState } from "react";

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

const ALL_CERTS = [
  "residential_glazing",
  "commercial_storefront",
  "auto_glass",
  "tempered_safety",
  "screen_repair",
  "shower_door",
];

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

export function ConfigPanel({
  world,
  onClose,
  onChanged,
}: {
  world: World;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [rows, setRows] = useState<Row[]>([...world.workers.map(rowFrom)]);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");

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
    <div className="config">
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

        <h3 className="config__vanshead">Vans</h3>
        <div className="config__vans">
          {world.vans.map((van) => (
            <span key={van.id}>
              {van.id}
              <button
                className="danger"
                disabled={busy || world.vans.length <= 1}
                title={world.vans.length <= 1 ? "the last van stays" : `retire ${van.id}`}
                onClick={() => void run(() => api.removeVan(van.id), `${van.id} removed`)}
              >
                Remove
              </button>
            </span>
          ))}
          <button
            disabled={busy}
            onClick={() => void run(() => api.configVan({ id: "", label: "" }), "van added")}
          >
            + Add a van
          </button>
        </div>

        {status && <p className="config__status">{status}</p>}
      </div>
    </div>
  );
}
