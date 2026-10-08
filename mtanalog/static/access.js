const token=new URLSearchParams(location.hash.slice(1)).get('access');
// Fragment stays out of HTTP requests and is removed before any navigation.
history.replaceState(null,'',location.pathname);
if(token){
  document.getElementById('access-message').textContent='Проверяем доступ…';
  fetch('/api/access',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token}),signal:AbortSignal.timeout(10000)})
    .then(async response=>{
      const result=await response.json();
      if(response.ok){location.replace('/');return;}
      document.getElementById('access-title').textContent='Доступ завершён';
      document.getElementById('access-message').textContent=result.error||'Ссылка больше не действует.';
    }).catch(()=>{document.getElementById('access-message').textContent='Не удалось проверить доступ. Откройте исходную ссылку ещё раз.';});
}
