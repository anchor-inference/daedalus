// A service on a phone: a QR for each way a phone can reach it, the same Wi-Fi first, because that costs
// nothing and goes nowhere. It lives in the service's Access sheet and works on any installation.
//
// A private service stays private: its QR carries a code the host mints on each press, spent once. So
// the codes are asked for on a press and never on opening the sheet, which would mint one for nobody.

import { useState } from "react";
import { api } from "./api";
import { errorText } from "./ui";
import { Icon } from "./icons";
import { t } from "./i18n";
import { ADDRESS, AddressLink, PublicAddress } from "./publicaddress";
import { useQuery } from "./store";
import { QrCode } from "./twofactor";
import "./publicaddress.css";

export type PhoneWay = { kind: "wifi" | "tunnel" | "address"; url: string; qr: string[] };
export type PhoneAnswer = { ways: PhoneWay[]; mode: "local" | "public" | "key"; code_minutes: number; pass_hours: number };

/** Same Wi-Fi first whatever order they came in: it is the way that exposes nothing. */
export function orderedWays(ways: PhoneWay[]): PhoneWay[] {
  return [...ways.filter((way) => way.kind === "wifi"), ...ways.filter((way) => way.kind !== "wifi")];
}

function NoWay() {
  const { data } = useQuery<PublicAddress>(ADDRESS, { staleMs: 5000 });
  if (!data) return null;
  return <div className="sub" data-phone-none="">{data.native ? <>{t("phone.none.native")} <AddressLink /></> : t("phone.none")}</div>;
}

export function ServicePhone({ sessionId, name, port, toast }: { sessionId: string; name: string; port: number | null | undefined; toast: (text: string) => void }) {
  const [answer, setAnswer] = useState<PhoneAnswer | null>(null);
  const [busy, setBusy] = useState(false);
  const path = `/api/sessions/${sessionId}/services/${encodeURIComponent(name)}/phone`;
  if (!port) return null;

  async function open() {
    setBusy(true);
    try {
      setAnswer(await api.post<PhoneAnswer>(path));
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  async function forget() {
    setBusy(true);
    try {
      await api.delete(path);
      // The host dropped the codes not yet used as well, so a QR left on screen would be a dead one.
      setAnswer(null);
      toast(t("phone.forgotten"));
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="service-phone" data-service-phone="">
      <div className="service-phone-actions">
        <button type="button" className="btn small" disabled={busy} onClick={() => void open()} data-phone-open=""><Icon name="phone" size={13} /> {t(answer?.ways.length ? "phone.again" : "phone.open")}</button>
        <button type="button" className="btn small" disabled={busy} onClick={() => void forget()} data-phone-forget="">{t("phone.forget")}</button>
      </div>
      {answer && (answer.ways.length ? (
        <>
          {/* Above the codes: on a phone two of them fill the sheet and a line under them is never read. */}
          {answer.mode === "local" && <div className="sub" data-phone-terms="">{t("phone.terms", { minutes: answer.code_minutes, hours: answer.pass_hours })}</div>}
          <div className="service-phone-ways">
            {orderedWays(answer.ways).map((way) => (
              <div key={way.kind} className="service-phone-way" data-way={way.kind}>
                <div className="sub">{t(way.kind === "wifi" ? "phone.way.wifi" : "phone.way.address")}</div>
                <QrCode rows={way.qr} label={t(way.kind === "wifi" ? "phone.way.wifi" : "phone.way.address")} />
              </div>
            ))}
          </div>
        </>
      ) : <NoWay />)}
    </div>
  );
}
