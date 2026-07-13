document.querySelector('.menu-button')?.addEventListener('click', () => document.querySelector('.nav-links')?.classList.toggle('open'));
document.querySelector('.access-form')?.addEventListener('submit', (event) => { event.preventDefault(); const success = document.querySelector('.success'); if (success) success.style.display = 'block'; event.currentTarget.reset(); });
