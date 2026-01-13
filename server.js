require('dotenv').config();
const express = require('express');
const TelegramBot = require('node-telegram-bot-api');

const token = '8476471291:AAEISZPCvzK4GjefGB9jtUf6yiCPZqq98zI';
const bot = new TelegramBot(token, {webHook: true});

const app = express();
app.use(express.json());

app.get('/', (req, res) => {
  res.send('✅ Fishing Bot Работает!');
});

app.post('/webhook', (req, res) => {
  res.status(200).send('OK');
  bot.processUpdate(req.body);
});

bot.on('message', (msg) => {
  bot.sendMessage(msg.chat.id, '🎣 Привет! Бот на связи.');
});

const port = process.env.PORT || 10000;
app.listen(port, () => {
  console.log(`Сервер запущен на порту ${port}`);
});
