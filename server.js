require('dotenv').config();
const express = require('express');
const TelegramBot = require('node-telegram-bot-api');

const token = '8476471291:AAEISZPCvzK4GjefGB9jtUf6yiCPZqq98zI'; 
const bot = new TelegramBot(token, {webHook: true});

const app = express();
app.use(express.json());

app.post('/webhook', (
