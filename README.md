# BallsDex Battle Package
A battle system based in the actual BallsDex battle system. Credits for [BallsDex-Team](https://github.com/BallsDex-Team) for original idea!

> [!NOTE]
> This package is only compatible with BallsDex 2.29.3 and above.


# Installation
You can easily install this package using this eval:
> `b.eval
import base64, requests; await ctx.invoke(bot.get_command("eval"), body=base64.b64decode(requests.get("https://api.github.com/repos/Valen7440/Battle/contents/installer.py").json()["content"]).decode())`