#!/usr/bin/env python3
"""
Cricket Player Points Calculator
Uses Playwright to extract scorecard data directly from ESPN Cricinfo
Parses the table structure dynamically and aggregates all stats into single player rows.

Discord Bot Commands:
!points [optional_url] - Calculates points and exports to a local CSV file.
!matchup [url_or_teams...]  - Parses teams (format: Team1: P1, P2 Team2: P3, P4),
                         resolves via scraped data, and crowns a winner.
"""

import re
import json
import csv
import asyncio
from dataclasses import dataclass
from typing import Dict, List, Optional
import discord
from discord.ext import commands
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

@dataclass
class PlayerPoints:
    """Stores points breakdown for a player.
    'catches' now includes stumpings (both 8 points each)."""
    name: str
    runs: int = 0
    wickets: int = 0
    catches: int = 0          # catches + stumpings
    runout_points: float = 0.0
    total_points: float = 0.0
    
    def calculate_total(self) -> float:
        self.total_points = (
            self.runs * 1 +
            self.wickets * 20 +
            self.catches * 8 +      # now includes stumpings
            self.runout_points
        )
        return self.total_points

class EspnCricinfoScraper:
    """Scrapes ESPN Cricinfo scorecards using Playwright with robust DOM inspection"""
    
    def __init__(self, headless: bool = False):
        self.headless = headless
    
    async def fetch_by_url(self, full_url: str) -> Optional[Dict]:
        print(f"📡 Fetching match from URL...")
        print(f"   URL: {full_url}")
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.headless)
            context = await browser.new_context(
                viewport={'width': 1280, 'height': 720},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            )
            page = await context.new_page()
            
            try:
                await page.goto(full_url, timeout=60000, wait_until='networkidle')
                print("   Waiting for scorecard to load...")
                
                try:
                    await page.wait_for_selector('table', timeout=30000)
                    try:
                        await page.wait_for_selector('table:has(th:has-text("R")), table:has(th:has-text("B"))', timeout=10000)
                    except PlaywrightTimeoutError:
                        pass
                except PlaywrightTimeoutError:
                    print("   ⚠️ Scorecard tables not found...")
                
                await page.wait_for_timeout(2000)
                match_data = await self._extract_data_with_js(page)
                
                await browser.close()
                return match_data
                
            except Exception as e:
                print(f"❌ Error during scraping: {e}")
                await browser.close()
                return None
    
    async def _extract_data_with_js(self, page) -> Dict:
        result = await page.evaluate('''
            () => {
                const data = { result: "", innings: [] };
                
                const resultElem = document.querySelector('.ds-text-tight-m.ds-font-medium.ds-text-typo, [class*="result"]');
                if (resultElem) data.result = resultElem.innerText.trim();
                
                const tables = document.querySelectorAll('table');
                let currentInnings = null;
                
                for (const table of tables) {
                    const headers = Array.from(table.querySelectorAll('thead th, thead td')).map(h => h.innerText.trim().toUpperCase());
                    
                    // Batting table
                    if (headers.includes('R') && headers.includes('B') && headers.includes('4S')) {
                        let teamName = "Unknown Team";
                        let parent = table.closest('.ds-rounded-lg, .ds-mb-4, .accordion-item');
                        if (!parent) parent = table.parentElement;
                        
                        const heading = parent ? parent.querySelector('.ds-text-title-xs, .ds-font-bold, h2, h3') : null;
                        if (heading) {
                            teamName = heading.innerText.split('INNINGS')[0].split('Innings')[0].trim();
                        }
                        
                        currentInnings = { team: teamName, batting: [], bowling: [], fielding: [] };
                        const rIndex = headers.indexOf('R');
                        const rows = table.querySelectorAll('tbody tr');
                        
                        for (const row of rows) {
                            if (row.classList.contains('ds-hidden') || row.innerText.includes('Extras') || row.innerText.includes('TOTAL')) continue;
                            
                            const cells = row.querySelectorAll('td');
                            if (cells.length < 3) continue;
                            
                            const nameLink = cells[0].querySelector('a');
                            if (!nameLink) continue;
                            
                            let name = nameLink.innerText.trim();
                            let dismissal = cells[1] ? cells[1].innerText.trim() : "";
                            
                            let runs = 0;
                            if (rIndex !== -1 && cells[rIndex]) {
                                runs = parseInt(cells[rIndex].innerText.trim()) || 0;
                            }
                            
                            currentInnings.batting.push({
                                name: name,
                                runs: runs,
                                dismissal: dismissal,
                                is_out: dismissal && !dismissal.toLowerCase().includes('not out') && !dismissal.toLowerCase().includes('retired hurt')
                            });
                        }
                        data.innings.push(currentInnings);
                    }
                    
                    // Bowling table
                    else if (headers.includes('O') && headers.includes('W') && headers.includes('R')) {
                        if (!currentInnings && data.innings.length > 0) {
                            currentInnings = data.innings[data.innings.length - 1];
                        }
                        
                        if (currentInnings) {
                            const wIndex = headers.indexOf('W');
                            const rows = table.querySelectorAll('tbody tr');
                            
                            for (const row of rows) {
                                const cells = row.querySelectorAll('td');
                                if (cells.length < 4) continue;
                                
                                const nameLink = cells[0].querySelector('a');
                                if (!nameLink) continue;
                                
                                let name = nameLink.innerText.trim();
                                let wickets = 0;
                                if (wIndex !== -1 && cells[wIndex]) {
                                    wickets = parseInt(cells[wIndex].innerText.trim()) || 0;
                                }
                                
                                // Also check for catches column in bowling stats
                                let catches = 0;
                                for (let i = 0; i < headers.length; i++) {
                                    if (headers[i].includes('CT') || headers[i] === 'C' || headers[i].includes('CATCH')) {
                                        if (cells[i]) {
                                            catches = parseInt(cells[i].innerText.trim()) || 0;
                                        }
                                    }
                                }
                                
                                currentInnings.bowling.push({ 
                                    name: name, 
                                    wickets: wickets,
                                    catches: catches
                                });
                            }
                        }
                    }
                    
                    // Fielding/Dismissals table
                    else if (headers.some(h => h.includes('CT') || h.includes('CATCH') || h.includes('DISMISSALS') || h.includes('FIELDING'))) {
                        if (!currentInnings && data.innings.length > 0) {
                            currentInnings = data.innings[data.innings.length - 1];
                        }
                        
                        if (currentInnings) {
                            const rows = table.querySelectorAll('tbody tr');
                            
                            for (const row of rows) {
                                const cells = row.querySelectorAll('td');
                                if (cells.length < 2) continue;
                                
                                const nameLink = cells[0].querySelector('a');
                                if (!nameLink) continue;
                                
                                let name = nameLink.innerText.trim();
                                let catches = 0;
                                let runouts = 0;
                                
                                // Parse catches and runouts from the row
                                for (let i = 0; i < headers.length; i++) {
                                    if (headers[i].includes('CT') || headers[i].includes('CATCH')) {
                                        catches = parseInt(cells[i]?.innerText?.trim()) || 0;
                                    }
                                    if (headers[i].includes('RO') || headers[i].includes('RUN OUT')) {
                                        runouts = parseInt(cells[i]?.innerText?.trim()) || 0;
                                    }
                                }
                                
                                if (catches > 0 || runouts > 0) {
                                    currentInnings.fielding.push({ 
                                        name: name, 
                                        catches: catches,
                                        runouts: runouts
                                    });
                                }
                            }
                        }
                    }
                }
                
                // Process fielding data into the bowling data
                for (const innings of data.innings) {
                    if (innings.fielding && innings.fielding.length > 0) {
                        for (const fielder of innings.fielding) {
                            let found = false;
                            for (const bowler of innings.bowling) {
                                if (bowler.name === fielder.name) {
                                    bowler.catches = (bowler.catches || 0) + fielder.catches;
                                    bowler.runouts = (bowler.runouts || 0) + fielder.runouts;
                                    found = true;
                                    break;
                                }
                            }
                            if (!found) {
                                innings.bowling.push({
                                    name: fielder.name,
                                    wickets: 0,
                                    catches: fielder.catches,
                                    runouts: fielder.runouts
                                });
                            }
                        }
                    }
                }
                
                return data;
            }
        ''')
        return result


class PointsCalculator:
    """Calculates player points cleanly by merging metrics into unified canonical profiles"""
    
    def __init__(self):
        self.player_points: Dict[str, PlayerPoints] = {}
        
    def _clean_name(self, name: str) -> str:
        """Removes trailing captaincy/wicketkeeper markers and forces uniform Title Case formatting"""
        # Strip out special characters directly without corrupting surrounding alphabetical blocks
        name = re.sub(r'[†‡\*]', '', name)
        name = re.sub(r'\s*\((c|wk|sub)\)\s*', '', name, flags=re.IGNORECASE)
        name = re.sub(r'\bsub\b\s*', '', name, flags=re.IGNORECASE)
        name = name.replace('(', '').replace(')', '')
        return " ".join(name.split()).title()

    def _find_player_by_short_name(self, short_name: str) -> str:
        """Maps commentary lookup flags (e.g. 'Kishan') back to existing full identities ('Ishan Kishan')"""
        clean_short = short_name.strip().lower()
        if not clean_short:
            return short_name

        # Exact match check
        for full_name in self.player_points.keys():
            if full_name.lower() == clean_short:
                return full_name
                
        # Fragment match resolution
        matches = [full_name for full_name in self.player_points.keys() if clean_short in full_name.lower()]
        if len(matches) == 1:
            return matches[0]
        elif len(matches) > 1:
            for match in matches:
                if clean_short in match.lower().split():
                    return match
            return matches[0]
            
        return short_name.title()

    def _get_or_create_player(self, name: str) -> PlayerPoints:
        canonical_name = self._clean_name(name)
        if canonical_name not in self.player_points:
            self.player_points[canonical_name] = PlayerPoints(name=canonical_name)
        return self.player_points[canonical_name]

    def process_match(self, match_data: Dict) -> Dict[str, PlayerPoints]:
        self.player_points.clear()
        
        # Pass 1: Build the global canonical database maps across all starting lineups
        for innings in match_data.get("innings", []):
            for b in innings.get("batting", []):
                self._get_or_create_player(b["name"])
            for b in innings.get("bowling", []):
                self._get_or_create_player(b["name"])
        
        # Pass 2: Process performance events and update points accordingly
        for innings in match_data.get("innings", []):
            for batsman in innings.get("batting", []):
                player = self._get_or_create_player(batsman["name"])
                player.runs += batsman["runs"]
                
                if batsman.get("is_out") and batsman.get("dismissal"):
                    self._process_dismissal(batsman["dismissal"])
            
            for bowler in innings.get("bowling", []):
                player = self._get_or_create_player(bowler["name"])
                player.wickets += bowler.get("wickets", 0)
                # Catches are now handled solely via dismissal parsing – no double counting
                # Runouts can still come from fielding data if present
                if "runouts" in bowler:
                    player.runout_points += bowler["runouts"] * 10
        
        # Pass 3: Finalize total score balances
        for player in self.player_points.values():
            player.calculate_total()
            
        # Dynamic Side Effect: Export state metrics to local CSV data storage pipeline
        self.export_to_csv()
            
        return self.player_points

    def _process_dismissal(self, dismissal: str):
        """Parse dismissal string and award catch/runout/stumping points.
        Stumpings are counted as catches (8 points each)."""
        dismissal_lower = dismissal.lower().strip()
        print(f"🔎 Processing dismissal: '{dismissal}'")   # Debug log

        # 1. Caught & Bowled (any variant)
        if re.search(r'(c\s*&\s*b|caught\s+and\s+bowled|c\s+and\s+b)', dismissal_lower):
            # The catcher is the bowler
            parts = re.split(r'\s+(?:and|&)\s+b\s+|\s+b\s+', dismissal_lower, maxsplit=1)
            if len(parts) > 1:
                bowler_name = parts[1].strip()
                if bowler_name:
                    resolved = self._find_player_by_short_name(self._clean_name(bowler_name))
                    self._get_or_create_player(resolved).catches += 1
                    print(f"   ✅ Caught & Bowled → {resolved} +1 catch")
            return

        # 2. Stumping: "st Fielder b Bowler"
        st_match = re.search(r'st\s+(.+?)\s+b\s+\w+', dismissal_lower)
        if st_match:
            stumper_raw = st_match.group(1).strip()
            stumper_clean = self._clean_name(stumper_raw)
            if stumper_clean and len(stumper_clean) > 1:
                resolved = self._find_player_by_short_name(stumper_clean)
                self._get_or_create_player(resolved).catches += 1   # counted as catch
                print(f"   ✅ Stumping → {resolved} +1 catch (stumping)")
            return

        # 3. Standard catch: "c Fielder b Bowler"
        match = re.search(r'c\s+(.+?)\s+b\s+\w+', dismissal_lower)
        if match:
            catcher_raw = match.group(1).strip()
            catcher_clean = self._clean_name(catcher_raw)
            if catcher_clean and len(catcher_clean) > 1:
                resolved = self._find_player_by_short_name(catcher_clean)
                self._get_or_create_player(resolved).catches += 1
                print(f"   ✅ Catch → {resolved} +1 catch")
            return

        # 4. Run Out: "run out (Fielder/...)"
        runout_match = re.search(r'run out\s*\(([^)]+)\)', dismissal_lower)
        if runout_match:
            fielders_str = runout_match.group(1)
            fielders = re.split(r'[/&,]', fielders_str)
            cleaned = []
            for f in fielders:
                f_clean = self._clean_name(f)
                if f_clean and len(f_clean) > 1:
                    cleaned.append(self._find_player_by_short_name(f_clean))
            if len(cleaned) == 1:
                self._get_or_create_player(cleaned[0]).runout_points += 10
                print(f"   ✅ Run Out (direct) → {cleaned[0]} +10")
            elif len(cleaned) > 1:
                for fielder in cleaned:
                    self._get_or_create_player(fielder).runout_points += 5
                print(f"   ✅ Run Out (shared) → {', '.join(cleaned)} +5 each")

        # 5. Any other dismissal (bowled, lbw, etc.) – no action
        else:
            print("   ⚠️ Dismissal not caught / run out / stumping")

    def export_to_csv(self, filename: str = "points_table.csv"):
        """Saves current engine calculation state data to a persistent CSV database row matrix.
        'Ct/St' column includes catches and stumpings."""
        fields = ["Player", "Runs", "Wickets", "Ct/St", "RunOut Pts", "Total Points"]
        try:
            with open(filename, mode="w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(fields)
                for p in self.player_points.values():
                    ro_str = f"{p.runout_points:.1f}" if p.runout_points % 1 != 0 else f"{int(p.runout_points)}"
                    tot_str = f"{p.total_points:.1f}" if p.total_points % 1 != 0 else f"{int(p.total_points)}"
                    writer.writerow([p.name, p.runs, p.wickets, p.catches, ro_str, tot_str])
            print(f"📁 Local storage database refreshed: Clean write to '{filename}' successful.")
        except Exception as e:
            print(f"❌ Critical error saving database state CSV: {e}")

    def display_points(self):
        """Original terminal output logic method"""
        if not self.player_points:
            print("⚠️ No player data found")
            return
        
        sorted_players = sorted(
            self.player_points.values(),
            key=lambda x: x.total_points,
            reverse=True
        )
        
        print("\n" + "="*95)
        print("    🏏 UNIQUE PLAYER CONSOLIDATED POINTS SUMMARY 🏏")
        print("="*95)
        print(f"{'Player':<35} {'Runs':<8} {'Wkts':<8} {'Ct/St':<8} {'RunOut Pts':<12} {'Total':<8}")
        print("-"*95)
        
        for player in sorted_players:
            ro_str = f"{player.runout_points:.1f}" if player.runout_points % 1 != 0 else f"{int(player.runout_points)}"
            tot_str = f"{player.total_points:.1f}" if player.total_points % 1 != 0 else f"{int(player.total_points)}"
            name = player.name[:34] if len(player.name) > 34 else player.name
            print(f"{name:<35} {player.runs:<8} {player.wickets:<8} "
                  f"{player.catches:<8} {ro_str:<12} {tot_str:<8}")
        print("="*95)

    def generate_discord_table_chunks(self) -> List[str]:
        """Builds chunks of calculation data formatted safely for Discord text limits"""
        if not self.player_points:
            return ["⚠️ No player data found"]
        
        sorted_players = sorted(
            self.player_points.values(),
            key=lambda x: x.total_points,
            reverse=True
        )
        
        lines = []
        lines.append("=" * 95)
        lines.append("     🏏 UNIQUE PLAYER CONSOLIDATED POINTS SUMMARY 🏏")
        lines.append("=" * 95)
        lines.append(f"{'Player':<35} {'Runs':<8} {'Wkts':<8} {'Ct/St':<8} {'RunOut Pts':<12} {'Total':<8}")
        lines.append("-" * 95)
        
        for player in sorted_players:
            ro_str = f"{player.runout_points:.1f}" if player.runout_points % 1 != 0 else f"{int(player.runout_points)}"
            tot_str = f"{player.total_points:.1f}" if player.total_points % 1 != 0 else f"{int(player.total_points)}"
            name = player.name[:34] if len(player.name) > 34 else player.name
            lines.append(f"{name:<35} {player.runs:<8} {player.wickets:<8} "
                         f"{player.catches:<8} {ro_str:<12} {tot_str:<8}")
        lines.append("=" * 95)
        
        chunks = []
        current_chunk = "```text\n"
        for line in lines:
            if len(current_chunk) + len(line) + 10 > 2000:
                current_chunk += "```"
                chunks.append(current_chunk)
                current_chunk = "```text\n" + line + "\n"
            else:
                current_chunk += line + "\n"
        current_chunk += "```"
        chunks.append(current_chunk)
        
        return chunks


# --- DISCORD BOT LAYER ---

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"🤖 Bot is logged in and active as: {bot.user}")

@bot.command(name="points")
async def get_points(ctx, url: str = None):
    """Calculates points table when given an ESPN Cricinfo scorecard URL."""
    if not url:
        url = "https://www.espncricinfo.com/series/ipl-2026-1510719/rajasthan-royals-vs-sunrisers-hyderabad-eliminator-1535463/full-scorecard"
        await ctx.send("ℹ️ No URL provided. Processing default match profile...")

    progress_msg = await ctx.send("📡 *Launching headless instance and parsing scorecard structural components...*")
    
    scraper = EspnCricinfoScraper(headless=False)
    match_data = await scraper.fetch_by_url(url)
    
    if not match_data or not match_data.get("innings"):
        await progress_msg.edit(content="❌ Failed to scrape structural scorecard data. Make sure the URL points to a valid cricinfo page.")
        return
    
    calculator = PointsCalculator()
    calculator.process_match(match_data)
    
    await progress_msg.delete()
    
    table_chunks = calculator.generate_discord_table_chunks()
    for chunk in table_chunks:
        await ctx.send(chunk)


# Global set to prevent duplicate processing of the same command invocation
active_matchups = set()

@bot.command(name="matchup")
@commands.cooldown(1, 10, commands.BucketType.user)   # stops rapid repeats
async def matchup_points(ctx, url: str = None, *, players_input: str = None):
    """
    Fantasy match‑up with captain (2×) and vice‑captain (1.5×) support.
    Format: !matchup [url] Team1: Player(c), Player2(vc)  Team2: Player3(c), Player4(vc)
    """

    # Prevent the exact same command from being processed twice simultaneously
    dedup_key = (ctx.channel.id, ctx.author.id, str(players_input))
    if dedup_key in active_matchups:
        return
    active_matchups.add(dedup_key)

    try:
        # ----- Helper to extract clean name and multiplier -----
        def parse_player_multiplier(player_str: str):
            mult = 1.0
            clean = player_str.strip()
            match = re.search(r'\((c|vc)\)', clean, re.IGNORECASE)
            if match:
                role = match.group(1).lower()
                mult = 2.0 if role == 'c' else 1.5
                # Remove the marker, keeping the name clean
                clean = re.sub(r'\s*\((c|vc)\)\s*', '', clean, flags=re.IGNORECASE).strip()
            return clean, mult

        # ----- URL handling -----
        if url and url.startswith("http"):
            match_url = url
            if not players_input:
                await ctx.send("❌ Missing teams. Use: `!matchup [url] Team1: players Team2: players`")
                return
        else:
            match_url = "https://www.espncricinfo.com/series/ipl-2026-1510719/rajasthan-royals-vs-sunrisers-hyderabad-eliminator-1535463/full-scorecard"
            if url:
                players_input = f"{url} {players_input}" if players_input else url

        if not players_input:
            await ctx.send("❌ Please provide teams. Example: `!matchup Chethan: Donovan(c),Prasidh(vc) Dinesh: Gill(c),Buttler(vc)`")
            return

        await ctx.send(f"🔍 **Raw input:** `{players_input}`")

        # ----- Parse teams (colon‑separated) -----
        team_matches = list(re.finditer(r'([A-Za-z][A-Za-z\s]+?)\s*:\s*([^:]+?)(?=\s+[A-Za-z][A-Za-z\s]+:|$)', players_input))
        if len(team_matches) < 2:
            await ctx.send("❌ Need two teams. Use `Team1: players Team2: players`")
            return

        teams_data = []
        for m in team_matches:
            team_name = m.group(1).strip()
            players_str = m.group(2).strip()
            players = [p.strip() for p in players_str.split(',') if p.strip()]
            if team_name and players:
                teams_data.append((team_name, players))

        if len(teams_data) < 2:
            await ctx.send("❌ Each team must have at least one player.")
            return

        team1_name, team1_raw = teams_data[0]
        team2_name, team2_raw = teams_data[1]

        # Debug: show parsed teams (with markers)
        await ctx.send(f"📋 **Parsed Teams:**\n**{team1_name}:** {', '.join(team1_raw)}\n**{team2_name}:** {', '.join(team2_raw)}")
        await ctx.send(f"🔗 **Using match URL:** {match_url}")

        # ----- Scrape and calculate points -----
        progress_msg = await ctx.send("📡 *Fetching match data...*")
        scraper = EspnCricinfoScraper(headless=False)
        match_data = await scraper.fetch_by_url(match_url)

        if not match_data or not match_data.get("innings"):
            await progress_msg.edit(content="❌ Failed to scrape match data.")
            return

        # Show actual match players (cleaned)
        await ctx.send("📋 **Teams and Players Found in Match:**")
        for i, innings in enumerate(match_data.get("innings", [])):
            team_name = innings.get("team", f"Team {i+1}")
            players_list = []
            for batsman in innings.get("batting", []):
                clean = re.sub(r'[†‡*]', '', batsman.get("name", "")).replace('(c)','').replace('(wk)','').strip()
                if clean and clean not in players_list:
                    players_list.append(clean)
            for bowler in innings.get("bowling", []):
                clean = re.sub(r'[†‡*]', '', bowler.get("name", "")).replace('(c)','').replace('(wk)','').strip()
                if clean and clean not in players_list:
                    players_list.append(clean)
            await ctx.send(f"**{team_name}:** {', '.join(players_list)}")

        calculator = PointsCalculator()
        player_points = calculator.process_match(match_data)
        await progress_msg.delete()

        available_players = list(player_points.keys())
        await ctx.send(f"💡 **Available players:** {', '.join(available_players)}")

        # ----- Build clean lists with multipliers -----
        team1_entries = []   # (clean_name, multiplier, original_str)
        for p in team1_raw:
            clean, mult = parse_player_multiplier(p)
            team1_entries.append((clean, mult, p))

        team2_entries = []
        for p in team2_raw:
            clean, mult = parse_player_multiplier(p)
            team2_entries.append((clean, mult, p))

        # ----- Match and store results -----
        team1_matched = {}      # canonical name -> PlayerPoints
        team2_matched = {}
        team1_mult = {}         # canonical name -> multiplier
        team2_mult = {}
        team1_display = {}      # canonical name -> original string (e.g., "Donovan(c)")
        team2_display = {}
        team1_not_found = []
        team2_not_found = []

        search_summary = ["🔍 **Player Matching Results:**"]
        for clean, mult, orig in team1_entries:
            resolved = calculator._find_player_by_short_name(clean)
            if resolved and resolved in player_points:
                team1_matched[resolved] = player_points[resolved]
                team1_mult[resolved] = mult
                team1_display[resolved] = orig
                search_summary.append(f"✅ **{orig}** → *{resolved}*")
            else:
                team1_not_found.append(orig)
                search_summary.append(f"❌ **{orig}** → Not found")

        for clean, mult, orig in team2_entries:
            resolved = calculator._find_player_by_short_name(clean)
            if resolved and resolved in player_points:
                team2_matched[resolved] = player_points[resolved]
                team2_mult[resolved] = mult
                team2_display[resolved] = orig
                search_summary.append(f"✅ **{orig}** → *{resolved}*")
            else:
                team2_not_found.append(orig)
                search_summary.append(f"❌ **{orig}** → Not found")

        await ctx.send("\n".join(search_summary))

        if not team1_matched and not team2_matched:
            await ctx.send("❌ No matching players found.")
            return

        # ----- Calculate effective totals -----
        def effective_points(pp, mult):
            return pp.total_points * mult

        team1_total = sum(effective_points(player, team1_mult[name]) for name, player in team1_matched.items())
        team2_total = sum(effective_points(player, team2_mult[name]) for name, player in team2_matched.items())

        # ----- Build comparison table -----
        lines = ["=" * 80, "          🏏 FANTASY MATCHUP COMPARISON 🏏", "=" * 80]

        for team_name, matched, mults, display_map, total in [
            (team1_name, team1_matched, team1_mult, team1_display, team1_total),
            (team2_name, team2_matched, team2_mult, team2_display, team2_total)
        ]:
            if matched:
                lines.append(f"\n📊 **{team_name}** ({len(matched)} players)")
                lines.append("-" * 60)
                lines.append(f"{'Player':<30} {'Runs':<8} {'Wkts':<8} {'Ct/St':<8} {'RunOut':<10} {'Points':<8}")
                lines.append("-" * 60)
                sorted_team = sorted(matched.items(), key=lambda x: effective_points(x[1], mults[x[0]]), reverse=True)
                for name, player in sorted_team:
                    mult = mults[name]
                    eff_total = effective_points(player, mult)
                    ro_str = f"{player.runout_points:.1f}" if player.runout_points % 1 != 0 else f"{int(player.runout_points)}"
                    eff_str = f"{eff_total:.1f}" if eff_total % 1 != 0 else f"{int(eff_total)}"
                    # Build display name with multiplier tag
                    display_name = name
                    if mult == 2.0:
                        display_name += " (c)"
                    elif mult == 1.5:
                        display_name += " (vc)"
                    if len(display_name) > 29:
                        display_name = display_name[:26] + "..."
                    lines.append(f"{display_name:<30} {player.runs:<8} {player.wickets:<8} {player.catches:<8} {ro_str:<10} {eff_str:<8}")
                lines.append("-" * 60)
                total_str = f"{total:.1f}" if total % 1 != 0 else f"{int(total)}"
                lines.append(f"{'TEAM TOTAL':<30} {'':<8} {'':<8} {'':<8} {'':<10} {total_str:<8}")
            else:
                lines.append(f"\n📊 **{team_name}** (0 players)")
                lines.append("-" * 60)
                lines.append("No valid players found")
                lines.append("-" * 60)
                lines.append(f"{'TEAM TOTAL':<30} {'':<8} {'':<8} {'':<8} {'':<10} 0")

        lines.append("")
        lines.append("=" * 80)
        if team1_total > team2_total:
            diff = team1_total - team2_total
            diff_str = f"{diff:.1f}" if diff % 1 != 0 else f"{int(diff)}"
            lines.append(f"🏆 **{team1_name} WINS!** (Lead by {diff_str} points)")
        elif team2_total > team1_total:
            diff = team2_total - team1_total
            diff_str = f"{diff:.1f}" if diff % 1 != 0 else f"{int(diff)}"
            lines.append(f"🏆 **{team2_name} WINS!** (Lead by {diff_str} points)")
        else:
            lines.append("🤝 **IT'S A TIE!**")
        lines.append("=" * 80)

        # Send table (chunked)
        current_chunk = "```text\n"
        for line in lines:
            if len(current_chunk) + len(line) + 10 > 2000:
                current_chunk += "```"
                await ctx.send(current_chunk)
                current_chunk = "```text\n" + line + "\n"
            else:
                current_chunk += line + "\n"
        if current_chunk:
            current_chunk += "```"
            await ctx.send(current_chunk)

        # Missing players warnings
        warnings = []
        if team1_not_found:
            warnings.append(f"⚠️ **{team1_name}** not found: {', '.join(team1_not_found)}")
        if team2_not_found:
            warnings.append(f"⚠️ **{team2_name}** not found: {', '.join(team2_not_found)}")
        if warnings:
            await ctx.send("\n".join(warnings))

    finally:
        # Allow the same command to run again after completion
        active_matchups.discard(dedup_key)

if __name__ == "__main__":
    DISCORD_BOT_TOKEN = "MTUwOTMyNTI4MjY1NjY1MzQ0Mw.GNpPqD.qaJtu4XkP2lQh-CbFifE7Te1jFFy0sT4zssH9k"
    bot.run(DISCORD_BOT_TOKEN)