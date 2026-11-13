// SPDX-License-Identifier: Apache-2.0
// Copyright 2025 Enclave
//
// The product's name. The app writes it into the page when it serves it, so the
// page shows it from its first moment; every script takes it from here and none
// spells it out.

const meta = document.querySelector('meta[name="application-name"]');

/** The name a person reads, as the app that served this page calls itself. */
export const PRODUCT = (meta && meta.getAttribute("content")) || document.title || "This app";
