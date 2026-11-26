"use strict";

const form = document.getElementById("claim-form");
const pendingClaimKeysStorage = "jamp.dashboard.pending-claims.v1";
const memoryPendingClaimKeys = new Map();

async function claimFingerprint(bodyText) {
  if (!globalThis.crypto?.subtle) {
    throw new Error("SECURE_IDEMPOTENCY_STORAGE_UNAVAILABLE");
  }
  const digest = await globalThis.crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(bodyText),
  );
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

function newIdempotencyKey() {
  if (globalThis.crypto?.randomUUID) {
    return globalThis.crypto.randomUUID();
  }
  if (globalThis.crypto?.getRandomValues) {
    const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0"));
    return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex.slice(6, 8).join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10).join("")}`;
  }
  throw new Error("SECURE_IDEMPOTENCY_KEY_UNAVAILABLE");
}

function readPendingClaimKeys() {
  try {
    const stored = sessionStorage.getItem(pendingClaimKeysStorage);
    const parsed = stored ? JSON.parse(stored) : {};
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
  } catch (_) {
    return {};
  }
}

async function getIdempotencyKey(bodyText) {
  const fingerprint = await claimFingerprint(bodyText);
  const pending = readPendingClaimKeys();
  const existing = pending[fingerprint] || memoryPendingClaimKeys.get(fingerprint);
  if (typeof existing === "string" && existing.length >= 8 && existing.length <= 128) {
    return { fingerprint, key: existing };
  }

  const key = newIdempotencyKey();
  pending[fingerprint] = key;
  memoryPendingClaimKeys.set(fingerprint, key);
  try {
    sessionStorage.setItem(pendingClaimKeysStorage, JSON.stringify(pending));
  } catch (_) {
    // Keep the key in memory for retries in this page; never send a new key on retry.
  }
  return { fingerprint, key };
}

function clearPendingClaimKey(fingerprint) {
  memoryPendingClaimKeys.delete(fingerprint);
  const pending = readPendingClaimKeys();
  delete pending[fingerprint];
  try {
    if (Object.keys(pending).length) {
      sessionStorage.setItem(pendingClaimKeysStorage, JSON.stringify(pending));
    } else {
      sessionStorage.removeItem(pendingClaimKeysStorage);
    }
  } catch (_) {
    // A stale key is safe: the server returns the original completed response.
  }
}

if (form) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const error = document.getElementById("claim-error");
    const button = document.getElementById("submit-claim");
    error.hidden = true;

    const kind = document.getElementById("subject-kind").value;
    const unit = document.getElementById("unit").value;
    if (!((kind === "crypto_asset" && unit === "USD") ||
          (kind === "equity_security" && unit === "RUB"))) {
      error.textContent = "Недопустимая пара типа объекта и валюты.";
      error.hidden = false;
      return;
    }

    const body = {
      text: document.getElementById("text").value,
      subject: {
        kind,
        identifier: document.getElementById("identifier").value,
      },
      predicate: "price",
      expected_value: Number(document.getElementById("expected-value").value),
      unit,
    };
    const bodyText = JSON.stringify(body);
    button.disabled = true;
    let fingerprint = null;
    try {
      const pending = await getIdempotencyKey(bodyText);
      fingerprint = pending.fingerprint;
      const response = await fetch("/claims/verify", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-JAMP-Dashboard-Request": "1",
          "Idempotency-Key": pending.key,
        },
        body: bodyText,
      });
      const result = await response.json();
      if (response.status === 201 && result.redirect_url) {
        clearPendingClaimKey(fingerprint);
        window.location.assign(result.redirect_url);
        return;
      }

      const retryable = response.status >= 500 ||
        (response.status === 409 &&
         result.error === "IDEMPOTENCY_REQUEST_IN_PROGRESS");
      if (!retryable) {
        clearPendingClaimKey(fingerprint);
      }
      error.textContent = String(response.status) + ": " +
        (result.error || "REQUEST_FAILED") +
        (retryable ? " Повторите отправку: ключ операции сохранён." : "");
    } catch (exception) {
      error.textContent = exception instanceof Error &&
        exception.message.startsWith("SECURE_IDEMPOTENCY")
        ? "Невозможно безопасно создать ключ операции. Запрос не отправлен."
        : "Dashboard transport failed. Ключ операции сохранён; повторите отправку без изменения данных.";
    } finally {
      button.disabled = false;
      error.hidden = false;
    }
  });
}

const replayButton = document.querySelector("[data-replay-id]");
if (replayButton) {
  replayButton.addEventListener("click", async () => {
    const id = replayButton.dataset.replayId;
    const output = document.getElementById("replay-result");
    const error = document.getElementById("replay-error");
    output.hidden = true;
    error.hidden = true;
    replayButton.disabled = true;
    try {
      const response = await fetch(
        "/verdicts/" + encodeURIComponent(id) + "/replay",
        {
          method: "POST",
          credentials: "same-origin",
          headers: { "X-JAMP-Dashboard-Request": "1" },
        },
      );
      const result = await response.json();
      if (!response.ok) {
        error.textContent = String(response.status) + ": " +
          (result.detail || result.error || "REPLAY_FAILED");
        error.hidden = false;
      } else {
        output.textContent = JSON.stringify(result, null, 2);
        output.hidden = false;
      }
    } catch (_) {
      error.textContent = "Replay transport failed. No successful replay is claimed.";
      error.hidden = false;
    } finally {
      replayButton.disabled = false;
    }
  });
}
