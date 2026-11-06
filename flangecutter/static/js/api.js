// Fetch helpers for the Flange Cutter API. Every failure becomes an Error with a plain-language
// message that the status bar can show as it is (the server sends {"error", "detail"}).

// A restarted app gets a new port and opens its own tab, so reloading this one would not help.
const NOT_RUNNING = 'Flange Cutter does not answer. Start it again: it opens in a new browser tab.';

async function call(url, options) {
  let response;
  try {
    response = await fetch(url, options);
  } catch {
    throw new Error(NOT_RUNNING);
  }
  if (response.ok) return response;
  let message = `The app reported an error (${response.status}).`;
  try {
    const body = await response.json();
    if (body.error) message = body.error;
  } catch { /* not JSON: keep the general message */ }
  throw new Error(message);
}

export async function getJSON(url) {
  return (await call(url)).json();
}

export async function postJSON(url, data) {
  const options = { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) };
  return (await call(url, options)).json();
}

export async function getBuffer(url) {
  return (await call(url)).arrayBuffer();
}

export async function uploadModel(file) {
  const form = new FormData();
  form.append('file', file);
  return (await call('/api/model', { method: 'POST', body: form })).json();
}
