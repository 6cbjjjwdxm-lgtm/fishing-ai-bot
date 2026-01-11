require('dotenv').config();
const express = require('express');
const TelegramBot = require('node-telegram-bot-api');
const OpenAI = require('openai');
const axios = require('axios');

const token = process.env.TELEGRAM_TOKEN;
const openaiKey = process.env.OPENAI_API_KEY;
const weatherKey = process.env.OPENWEATHER_API_KEY;

const bot = new TelegramBot(token, {webHook: true});
const openai = new OpenAI({apiKey: openaiKey});

const app = express();
app.use(express.json());

app.post('/webhook', (req, res) => {
  bot.processUpdate(req.body);
  res.sendStatus(200);
});

app.get('/', (req, res) => {
  res.send('✅ Fishing AI Bot webhook ready!');
});

bot.onText(/\/start/, (msg) => {
  bot.sendMessage(msg.chat.id, '🎣 Рыбалка с ИИ! Задавай вопросы о спиннинге.');
});

bot.on('message', async (msg) => {
  if (msg.text && !msg.text.startsWith('/')) {
    const chatId = msg.chat.id;
    const response = await openai.chat.completions.create({
      model: "gpt-4o-mini",
      messages: [{role: "user", content: msg.text}]
    });
    bot.sendMessage(chatId, response.choices[0].message.content);
  }
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
  console.log(`🚀 Server on port ${PORT}`);
  bot.setWebHook(`https://fishing-ai-bot.onrender.com/webhook`);
});
