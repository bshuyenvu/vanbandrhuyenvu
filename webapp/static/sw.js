const CACHE='hv-ai-word-v10-shell-1';
const SHELL=['/editor','/manifest.webmanifest','/api/v3/capabilities'];
self.addEventListener('install',event=>event.waitUntil(caches.open(CACHE).then(c=>c.addAll(SHELL)).then(()=>self.skipWaiting())));
self.addEventListener('activate',event=>event.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k)))).then(()=>self.clients.claim())));
self.addEventListener('fetch',event=>{
  const req=event.request;
  if(req.method!=='GET')return;
  const url=new URL(req.url);
  if(url.origin!==location.origin)return;
  if(url.pathname.startsWith('/api/')){event.respondWith(fetch(req).catch(()=>caches.match(req)));return;}
  event.respondWith(fetch(req).then(r=>{const clone=r.clone();caches.open(CACHE).then(c=>c.put(req,clone));return r}).catch(()=>caches.match(req).then(r=>r||caches.match('/editor'))));
});
