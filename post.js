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

  const resp = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ chat_id: chatId, text: next })
  });

  const data = await resp.json();

  if (!data.ok) {
    throw new Error('Telegram API error: ' + data.description);
  }

  console.log('Опубликовано:', next.slice(0, 60).replace(/\n/g, ' ') + '...');

  fs.writeFileSync('posts.json', JSON.stringify(posts, null, 2) + '\n');
}

main().catch(err => {
  console.error(err.message);
  process.exit(1);
});
