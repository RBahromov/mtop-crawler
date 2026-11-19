# mtop-crawler — bepul masofaviy yig'uvchi

mtop hosting sekin bo'lsa ham ko'p va tez ma'lumot yig'ish uchun: sahifalarni **GitHub Actions** (bepul) yuklaydi,
hosting faqat tayyor HTML/PDF ni qabul qilib indekslaydi. Hosting hech qayerga o'zi chiqmaydi.

## O'rnatish (10 daqiqa)

1. **Hostingda:** yangi `mtop-update.zip` ni joylang (ichida `ingest.php` bor). Tekshiring:
   `https://SIZNING-SAYT/mtop/ingest.php` brauzerda ochilsa `{"ok":false,"error":"faqat POST"}` chiqishi kerak.
2. **GitHub'da:** yangi **public** repozitoriy yarating (ommaviy repoda Actions daqiqalari cheksiz bepul).
   Shu papkadagi hamma fayllarni (`mtop_worker.py`, `.github/`, `README.md`) yuklang.
   Kod ichida hech qanday sir yo'q — kalitlar faqat Secrets'da saqlanadi.
3. Repo → **Settings → Secrets and variables → Actions → New repository secret**:
   - `MTOP_URL` = `https://SIZNING-SAYT/mtop` (oxirida `/` bo'lmasin)
   - `MTOP_KEY` = `CRAWL_KEY` (admin → Navbat sahifasida ko'rinadi)
4. **Actions** bo'limida workflow'larni yoqing, so'ng:
   - **mtop-seed → Run workflow** (limit 20000) — Common Crawl'dan tayyor `.uz` URL ro'yxatini navbatga qo'shadi.
   - **mtop-crawl → Run workflow** — yig'ishni boshlaydi. Keyin har 6 soatda o'zi ishga tushadi.
5. Admin → Navbat: «Masofaviy yig'uvchi» kartasida oxirgi faollik va yig'ilgan sahifalar soni ko'rinadi.

## Qoidalar va xavfsizlik
- `robots.txt` va `Crawl-delay` hurmat qilinadi; bir saytga kamida 1 soniya pauza; User-Agent: `MtopBot/1.0`.
- Faqat ruxsat etilgan domenlar (.uz yoki admin ruxsati) hostingda qabul qilinadi; ichki/lokal manzillar yig'uvchi tomonida bloklanadi.
- Har so'rov HMAC-SHA256 bilan imzolanadi (vaqt oynasi ±10 daqiqa).
- Bir sahifa 2 MB (PDF 4 MB) dan katta bo'lsa o'tkazib yuboriladi.
- Yig'ishni modda saqlang: GitHub Actions'dan haddan tashqari agressiv foydalanish shartlarga zid bo'lishi mumkin.

## Boshqa joyda ishga tushirish (VPS / o'z kompyuter)
```
export MTOP_URL=https://SIZNING-SAYT/mtop MTOP_KEY=...
python3 mtop_worker.py crawl --minutes 600 --threads 16
python3 mtop_worker.py seed-cc --limit 50000
python3 mtop_worker.py seed-file urls.txt
python3 mtop_worker.py stats
```
Python 3.9+, qo'shimcha kutubxona shart emas.

## Muammolar
- «Hosting rad etdi (HTTP 401) — imzo noto'g'ri»: `MTOP_KEY` CRAWL_KEY bilan bir xil emas.
- «vaqt noto'g'ri»: hosting soati ±10 daqiqadan ko'p farq qiladi.
- «HTTP 405/503»: `ingest.php` joylanmagan yoki sayt o'rnatilmagan.
- Scheduled ishlar to'xtasa: Actions → mtop-crawl → Run workflow (keepalive ishi har oy qayta yoqadi).
