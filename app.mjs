import { createServer } from 'node:http';

const page = `<!doctype html><html lang="en"><meta charset="utf-8">
<title>Synthetic service desk</title>
<style>body{font:20px system-ui;max-width:650px;margin:60px auto;background:#f6f8fc;color:#14273d}input,button{font:inherit;padding:12px;margin:12px 0}button{background:#174f90;color:white;border:0;border-radius:6px}output{display:block;padding:20px;background:white}</style>
<h1>Synthetic service desk</h1><p>Local demo. No accounts or real citizen data.</p>
<label>Citizen note <input id="note"></label><br>
<button id="save">Save note</button> <button id="count">Count request</button>
<output role="status" id="status"></output>
<script>
const note = document.querySelector('#note');
const status = document.querySelector('#status');
const render = () => {
  note.value = localStorage.getItem('note') || '';
  status.textContent = 'Note: ' + (localStorage.getItem('note') || 'empty') + ' | Requests: ' + (localStorage.getItem('requests') || '0');
};
document.querySelector('#save').onclick = () => { localStorage.setItem('note', note.value); render(); };
document.querySelector('#count').onclick = () => { localStorage.setItem('requests', String(Number(localStorage.getItem('requests') || 0) + 1)); render(); };
render();
</script></html>`;
const server = createServer((_request, response) => {
  response.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
  response.end(page);
});
server.listen(0, '127.0.0.1', () => {
  const { port } = server.address();
  console.log(JSON.stringify({ pid: process.pid, port, url: `http://127.0.0.1:${port}/` }));
});
