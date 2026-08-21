import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import {
  dataRequestSchema,
  dataResponseSchema,
  responseMatchesRequest,
  takeoverContextBodySchema,
  takeoverContextSchema
} from "../src/content-sync";

const srcDir = join(dirname(fileURLToPath(import.meta.url)), "../src");
const source = (name: string): string => readFileSync(join(srcDir, name), "utf8");

const request = {
  v: 1,
  type: "data.request",
  requestId: "request_AbCdEfGhIjKl",
  deviceId: "device_AbCdEfGhIjKl",
  resource: "takeover.context",
  params: { offerId: "offer_AbCdEfGhIjKl" },
  issuedAtUnixMs: 1_787_000_000_000,
  expiresAtUnixMs: 1_787_000_005_000
} as const;

const context = {
  v: 1,
  peerNumber: "+15105550123",
  claimedName: "Kevin",
  purpose: "约机主周六吃饭",
  updatedAtUnixMs: 1_787_000_001_000
} as const;

const response = (body: unknown) => ({
  v: 1,
  type: "data.response",
  requestId: request.requestId,
  resource: "takeover.context",
  status: "ok",
  body
});

describe("takeover context relay contract", () => {
  it("accepts a fully populated context and one with every optional field absent", () => {
    expect(takeoverContextSchema.safeParse(context).success).toBe(true);
    expect(takeoverContextSchema.safeParse({
      v: 1,
      peerNumber: "+15105550123",
      claimedName: null,
      purpose: null,
      updatedAtUnixMs: 1_787_000_001_000
    }).success).toBe(true);
  });

  it("requires all five keys — an omitted key is not the same as a null one", () => {
    const { claimedName: _omitted, ...missingKey } = context;
    expect(takeoverContextSchema.safeParse(missingKey).success).toBe(false);
  });

  it("enforces the length caps that keep the single-line call screen intact", () => {
    expect(takeoverContextSchema.safeParse({ ...context, claimedName: "a".repeat(60) }).success).toBe(true);
    expect(takeoverContextSchema.safeParse({ ...context, claimedName: "a".repeat(61) }).success).toBe(false);
    expect(takeoverContextSchema.safeParse({ ...context, purpose: "a".repeat(120) }).success).toBe(true);
    expect(takeoverContextSchema.safeParse({ ...context, purpose: "a".repeat(121) }).success).toBe(false);
  });

  it("wraps present and absent context in one shape so callers need a single parse path", () => {
    expect(takeoverContextBodySchema.safeParse({ context }).success).toBe(true);
    expect(takeoverContextBodySchema.safeParse({ context: null }).success).toBe(true);
    // A bare context object (no wrapper) is the shape that would force a second
    // parse branch; reject it at the boundary.
    expect(takeoverContextBodySchema.safeParse(context).success).toBe(false);
  });

  it("round-trips the relay envelope for both populated and empty context", () => {
    expect(dataRequestSchema.safeParse(request).success).toBe(true);
    for (const body of [{ context }, { context: null }]) {
      const parsed = dataResponseSchema.safeParse(response(body));
      expect(parsed.success).toBe(true);
      if (parsed.success) expect(responseMatchesRequest(parsed.data, dataRequestSchema.parse(request))).toBe(true);
    }
  });

  it("rejects a response whose requestId does not match the outstanding request", () => {
    const parsed = dataResponseSchema.parse(
      { ...response({ context }), requestId: "request_ZzZzZzZzZzZz" }
    );
    expect(responseMatchesRequest(parsed, dataRequestSchema.parse(request))).toBe(false);
  });

  it("rejects an offerId that is not an opaque offer id", () => {
    for (const offerId of ["call_AbCdEfGhIjKl", "offer_short", "../offer_AbCdEfGhIjKl", ""]) {
      expect(dataRequestSchema.safeParse({ ...request, params: { offerId } }).success).toBe(false);
    }
  });

  it("keeps the relay expiry window bounded like every other content request", () => {
    expect(dataRequestSchema.safeParse({
      ...request,
      expiresAtUnixMs: request.issuedAtUnixMs + 11_000
    }).success).toBe(false);
  });
});

describe("takeover context privacy boundary (ADR-003)", () => {
  it("never persists, audits, or logs the relayed body", () => {
    const handler = source("index.ts")
      .split("async function readTakeoverContext")[1]
      ?.split("async function relayContentRead")[0] ?? "";
    expect(handler).not.toContain("INSERT");
    expect(handler).not.toContain("UPDATE");
    expect(handler).not.toContain("audit(");
    expect(handler).not.toContain("console.");
  });

  it("checks offer ownership before relaying so ids cannot be fished across edges", () => {
    const handler = source("index.ts")
      .split("async function readTakeoverContext")[1]
      ?.split("async function relayContentRead")[0] ?? "";
    const ownershipCheck = handler.indexOf("offer.edge_id !== device.edge_id");
    const relayCall = handler.indexOf("content-relay");
    expect(ownershipCheck).toBeGreaterThan(-1);
    expect(relayCall).toBeGreaterThan(ownershipCheck);
  });

  it("is not gated by content sync — the takeover flow already grants live audio", () => {
    const handler = source("index.ts")
      .split("async function readTakeoverContext")[1]
      ?.split("async function relayContentRead")[0] ?? "";
    expect(handler).not.toContain("contentReadEnabled");
    expect(handler).not.toContain("requireContentCapability");
  });

  it("fails loudly if takeover.context is ever routed through the content capability gate", () => {
    const capability = source("index.ts")
      .split("async function requireContentCapability")[1]
      ?.split("\n}")[0] ?? "";
    expect(capability).toContain("takeover.context");
    expect(capability).toContain("INTERNAL_ERROR");
  });
});
