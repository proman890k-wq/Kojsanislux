const fs = require('fs');

async function main() {
  const posts = JSON.parse(fs.readFileSync('posts.json', 'utf8'));

  if (posts.length === 0) {
    console.log('Очередь пуста — постить нечего. Добавь новые посты в posts.json.');
    return;
  }

  const next = posts.shift();
  const token = process.env.BOT_TOKEN;
  const chatId = process.env.CHAT_ID;

  if (!token || !chatId) {
    throw new Error('Не заданы BOT_TOKEN или CHAT_ID (проверь секреты репозитория).');
  }

  let resp, data;

  if (next.type === 'photo') {
    const seed = Date.now();
    const imageUrl = `https://picsum.photos/seed/${seed}/800/450`;

    resp = await fetch(`https://api.telegram.org/bot${token}/sendPhoto`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        chat_id: chatId,
        photo: imageUrl,
        caption: next.text
      })
    });
  } else {
    resp = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_id: chatId, text: next.text })
    });
  }

  data = await resp.json();

  if (!data.ok) {
    throw new Error('Telegram API error: ' + data.description);
  }

  console.log('Опубликовано (' + next.type + '):', next.text.slice(0, 60).replace(/\n/g, ' ') + '...');

  fs.writeFileSync('posts.json', JSON.stringify(posts, null, 2) + '\n');

  const LOW_THRESHOLD = 5;
  const ownerId = process.env.OWNER_ID;

  if (ownerId && posts.length <= LOW_THRESHOLD) {
    const warnText = posts.length === 0
      ? '⚠️ Кожаный вслух: посты закончились! Очередь пуста, пора закинуть новую партию.'
      : `⚠️ Кожаный вслух: осталось всего ${posts.length} постов в очереди. Пора пополнить запас.`;

    await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_id: ownerId, text: warnText })
    });
  }
}

main().catch(err => {
  console.error(err.message);
  process.exit(1);
});
