// SPDX-License-Identifier: Apache-2.0
// Copyright 2025 Enclave
//
// The product's name. The app writes it into the page when it serves it, so the
// page shows it from its first moment; every script takes it from here and none
// spells it out. So with the name its entry has in another program's settings file.

const meta = document.querySelector('meta[name="application-name"]');
const entry = document.querySelector('meta[name="application-entry"]');

/** The name a person reads, as the app that served this page calls itself. */
export const PRODUCT = (meta && meta.getAttribute("content")) || document.title || "This app";

/**
 * The name of the product's entry in the settings file of an AI app, as the app that
 * served this page writes it. For a sentence that names the entry where no look at a
 * file has said it.
 */
export const ENTRY = (entry && entry.getAttribute("content")) || "";
