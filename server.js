require('dotenv').config();
const express = require('express');
const TelegramBot = require('node-telegram-bot-api');

const token = process.env.TELEGRAM_TOKEN || 'NO_TOKEN';
const bot = new TelegramBot(token, {webHook: true});

const app = express();
app.use(express.json());

app.post('/webhook', (req, res) => {
  bot.processUpdate(req.body);
  res.sendStatus(200);
});

app.get('/', (req, res) => {
  res.send('✅ Fishing Bot готов!');
});

bot.onText(/\/start/, (msg) => {
  bot.sendMessage(msg.chat.id, '🎣 Спиннинг бот готов! Пиши вопросы.');
});

bot.on('message', async (msg) => {
  if (msg.text && !msg.text.startsWith('/')) {
    bot.sendMessage(msg.chat.id, '🤖 Отвечаю через OpenAI...');
  }
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
  console.log(`Server: port ${PORT}`);
});
