import { useState } from "react";
import type { Booking } from "../types";
import { Modal } from "./Modal";
import { BookingStatusBadge } from "./Badge";
import { ArrowUpRightIcon, CheckCircleIcon, CopyIcon } from "./Icons";
import { fmtDateTime, fmtNumber, fmtPhone, fmtToken } from "../lib/format";

// Booking detail popup — opened by clicking a row in the bookings table.

function Fact({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="mt-0.5 text-sm text-ink-2">{value}</dd>
    </div>
  );
}

export function BookingModal({
  booking,
  onClose,
}: {
  booking: Booking;
  onClose: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const phone = booking.customerPhone?.replace(/\D/g, "") ?? null;

  async function copyPhone() {
    if (!phone) return;
    try {
      await navigator.clipboard.writeText(phone);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      /* clipboard unavailable */
    }
  }

  return (
    <Modal
      title={`Booking ${booking.id.slice(0, 12)}`}
      subtitle={<BookingStatusBadge status={booking.status} />}
      onClose={onClose}
      footer={
        phone ? (
          <>
            <button
              type="button"
              onClick={copyPhone}
              className="inline-flex items-center gap-1.5 rounded-md border border-hairline px-3 py-1.5 text-xs font-medium text-ink-2 hover:bg-page"
            >
              {copied ? <CheckCircleIcon /> : <CopyIcon />}
              {copied ? "Copied" : "Copy number"}
            </button>
            <a
              href={`https://wa.me/${phone}`}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-xs font-medium text-white hover:opacity-90"
            >
              <ArrowUpRightIcon />
              Message customer
            </a>
          </>
        ) : (
          <span className="text-xs text-muted">No phone number on record</span>
        )
      }
    >
      <dl className="grid grid-cols-2 gap-x-6 gap-y-4 px-5 py-4">
        <Fact label="Scheduled for" value={fmtDateTime(booking.datetime)} />
        <Fact label="Booked on" value={fmtDateTime(booking.createdAt)} />
        <Fact label="Customer" value={booking.customerName ?? "—"} />
        <Fact label="Phone" value={fmtPhone(booking.customerPhone)} />
        <Fact label="Service" value={booking.service ?? "—"} />
        <Fact label="Party size" value={fmtNumber(booking.partySize)} />
        <Fact label="Source" value={booking.source ? fmtToken(booking.source) : "—"} />
        <Fact label="Booking ID" value={<code className="text-xs">{booking.id}</code>} />
        {booking.notes ? (
          <div className="col-span-2">
            <dt className="text-xs text-muted">Notes / special requests</dt>
            <dd className="mt-0.5 whitespace-pre-wrap text-sm text-ink-2">{booking.notes}</dd>
          </div>
        ) : null}
      </dl>
    </Modal>
  );
}
