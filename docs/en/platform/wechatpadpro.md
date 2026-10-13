# Connect WeChatPadPro

The WeChatPadPro adapter sends and receives personal WeChat messages through a WeChatPadPro service you host. AstrBot uses the admin key to create an auth key, then waits for a QR-code login.

## Prepare

1. Deploy and start WeChatPadProMAX. The machine running AstrBot must be able to reach its HTTP port. The API prefix is `/api`.
2. Prepare the admin key. The service port is usually `8059`.

## Create a bot

1. Open the AstrBot WebUI.
2. Open `Bots` in the sidebar and click `Create Bot`.
3. Choose `WeChatPadPro`.
4. Fill in the host, port, and admin key, then enable and save the bot.
5. Read the QR-code URL from the AstrBot logs and scan it with WeChat. After login, the auth key is stored in `wechatpadpro_credentials.json` under the data directory.

## Settings

- `admin_key`: WeChatPadPro admin key.
- `host`: Service IP or domain, without a scheme or port.
- `wechatpadpro_port`: Service port. A legacy `port` value is still read.

Messages arrive on `ws://{host}:{port}/ws/sync`. Before each connection AstrBot requests a ticket from `/api/Runtime/WebSocketTicket`. The ticket is valid for 5 minutes and can be used once. A dropped connection requests a new ticket and connects again.

## Messages

Text, image, voice, emoji, and quote messages can be received. Sending supports text, image, emoji, and voice. Voice is converted to Tencent Silk. Private and group text both use `/Msg/SendTxt`. A group mention puts one or more member wxids in `At`, separated by commas, so several people can be mentioned in one message.
