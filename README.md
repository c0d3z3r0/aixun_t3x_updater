# AIXUN T3x updater

This tool allows updating of AiXun T3A/T3B/T320/T420D without installing their vendor software or sign-up.

Instructions:

1. Get firmware image either from [aixun_t3a_rev/bin](https://github.com/c0d3z3r0/aixun_t3a_rev/tree/master/bin) or like this `wget https://usr.tnyzeq.icu/~dd/aixun-updates/firmware/JC_M_T3A_1.36.bin`
2. Install dependencies: `pip install -r requirements.txt`
3. Connect your T3A/T3B via USB
4. Run the updater: `./t3xupdate.py JC_M_T3A_1.xx.bin`

## License

Copyright (c) 2024 Michael Niewöhner

This is open source software, licensed under GPLv2. See LICENSE file for details.
