// Skyblock networth of players, with SkyHelper-Networth (prices from its price list).
// Usage: HYPIXEL_KEY=... node networth.js <uuid> [<uuid> ...]  ->  JSON {uuid: result} on stdout
const { ProfileNetworthCalculator } = require('skyhelper-networth');

async function hypixel(path) {
    const r = await fetch(`https://api.hypixel.net/v2/${path}`, {
        headers: { 'API-Key': process.env.HYPIXEL_KEY || '', 'User-Agent': 'online-watch/1.1' },
    });
    if (!r.ok) throw new Error(`HTTP ${r.status} for ${path.split('?')[0]}`);
    return r.json();
}

async function networth(uuid) {
    const { profiles } = await hypixel(`skyblock/profiles?uuid=${uuid}`);
    if (!profiles || !profiles.length) return { error: 'no Skyblock profile' };
    // the profile the player last played on
    const profile = profiles.find((p) => p.selected) || profiles[0];
    const member = profile.members[uuid];
    let museum = {};
    try {
        museum = (await hypixel(`skyblock/museum?profile=${profile.profile_id}`)).members?.[uuid] || {};
    } catch (e) {
        // museum is optional
    }
    const calc = new ProfileNetworthCalculator(member, museum, profile.banking?.balance ?? 0);
    const nw = await calc.getNetworth({ onlyNetworth: true });
    const types = Object.entries(nw.types || {})
        .map(([k, v]) => [k, v.total || 0])
        .sort((a, b) => b[1] - a[1]);
    return {
        profile: profile.cute_name,
        networth: nw.networth,
        purse: nw.purse,
        bank: nw.bank,
        noInventory: nw.noInventory,
        top: types[0] && types[0][1] > 0 ? types[0] : null,
    };
}

(async () => {
    const out = {};
    for (const uuid of process.argv.slice(2)) {
        try {
            out[uuid] = await networth(uuid);
        } catch (e) {
            out[uuid] = { error: String(e.message || e).slice(0, 200) };
        }
    }
    process.stdout.write(JSON.stringify(out));
    process.exit(0); // the library keeps an update timer running
})();
