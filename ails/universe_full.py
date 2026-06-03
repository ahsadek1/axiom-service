"""
universe_full.py — Full S&P 500 + Nasdaq 100 Universe
=====================================================
Comprehensive ticker list for AILS backtest expansion.
Covers all S&P 500 sectors + Nasdaq 100 as of April 2026.
Already-populated tickers are skipped automatically by the populator.
"""

# ---------------------------------------------------------------------------
# FULL UNIVERSE — S&P 500 + Nasdaq 100 (~480 liquid tickers)
# Organized by sector for auditability
# ---------------------------------------------------------------------------

TECHNOLOGY = [
    # Mega-cap / semiconductors
    "AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "AMD", "QCOM", "INTC", "TXN", "MU",
    "AMAT", "LRCX", "KLAC", "MRVL", "CDNS", "SNPS", "MPWR", "SWKS", "QRVO", "ON",
    # Software / cloud
    "ADBE", "CRM", "NOW", "INTU", "PANW", "CRWD", "FTNT", "ANSS", "AKAM", "EPAM",
    "GDDY", "FFIV", "CTSH", "ACN", "IBM", "HPQ", "HPE", "DELL", "NTAP", "WDC",
    "STX", "KEYS", "TER", "ENPH", "SEDG", "FSLR",
    # Internet / platforms
    "META", "GOOGL", "GOOG", "NFLX", "AMZN",
]

COMMUNICATION_SERVICES = [
    "DIS", "CMCSA", "T", "VZ", "TMUS", "WBD", "FOXA", "FOX",
    "EA", "TTWO", "LYV", "OMC", "IPG", "NWSA", "NWS",
    "MTCH", "PINS", "SNAP", "ZM", "RBLX",
]

CONSUMER_DISCRETIONARY = [
    "TSLA", "HD", "MCD", "SBUX", "NKE", "TGT", "LOW", "TJX", "BKNG",
    "EBAY", "ETSY", "ULTA", "CMG", "YUM", "DPZ", "POOL", "DRI", "NVR",
    "PHM", "DHI", "LEN", "TOL", "MDC", "GRMN", "APTV", "VC", "LEA",
    "LKQ", "AZO", "ORLY", "AAP", "GPC", "KMX", "AN", "LAD",
    "HLT", "MAR", "H", "IHG", "RCL", "CCL", "NCLH", "MGM", "CZR",
    "LVS", "WYNN", "PENN", "DKNG", "F", "GM", "RACE",
    "AMZN", "EBAY", "W", "WSM", "RH", "BBWI",
]

CONSUMER_STAPLES = [
    "WMT", "COST", "PG", "KO", "PEP", "PM", "MO", "MDLZ", "CL", "EL",
    "KMB", "GIS", "K", "CAG", "CPB", "HSY", "SJM", "MKC", "MNST", "STZ",
    "BUD", "SAM", "TAP", "DEO", "BTI", "MO", "TSN", "HRL", "SFM", "KR",
    "SYY", "USFD",
]

HEALTHCARE = [
    # Managed care
    "UNH", "ELV", "CVS", "CI", "HUM", "MOH", "CNC", "HCA",
    # Pharma
    "LLY", "JNJ", "ABBV", "MRK", "PFE", "BMY", "AMGN", "GILD", "BIIB",
    "VRTX", "REGN", "MRNA", "BNTX", "AZN", "NVO", "SNY",
    # MedTech / devices
    "ISRG", "EW", "SYK", "BSX", "MDT", "ZBH", "BDX", "BAX", "TMO",
    "DHR", "A", "MTD", "WAT", "IDXX", "TECH", "PODD", "DXCM",
    "ALGN", "HSIC", "RMD", "VAR", "HOLX", "GEHC", "IQV", "CRL",
    # Biotech / specialty
    "ILMN", "SGEN", "EXAS", "NTRA", "IOVA", "FATE", "KYMR",
]

FINANCIALS = [
    # Banks
    "JPM", "BAC", "WFC", "C", "USB", "TFC", "PNC", "CFG", "FITB",
    "HBAN", "MTB", "RF", "KEY", "ZION", "CMA", "STT", "BK", "NTRS",
    # Capital markets
    "GS", "MS", "BLK", "SCHW", "ICE", "CME", "CBOE", "SPGI", "MCO",
    "RJF", "HOOD", "IBKR", "COIN", "MKTX",
    # Payments / fintech
    "AXP", "V", "MA", "PYPL", "FIS", "FISV", "GPN", "WEX", "FOUR",
    # Insurance
    "AIG", "MET", "PRU", "AFL", "ALL", "PGR", "TRV", "CB",
    "AON", "MMC", "WTW", "ACGL", "RE", "EG",
    # PE / alternatives
    "KKR", "APO", "BX", "CG", "ARES", "TPG", "BAM",
    # Specialty
    "COF", "SYF", "DFS", "ADS", "ALLY",
]

ENERGY = [
    # Integrated
    "XOM", "CVX", "COP",
    # E&P
    "EOG", "PXD", "FANG", "DVN", "MRO", "APA", "OXY", "HES", "EQT",
    "RRC", "AR", "CHK", "SWN", "CTRA", "SM", "REI",
    # Services
    "SLB", "HAL", "BKR", "FTI", "NOV", "NE",
    # Refining / midstream
    "MPC", "PSX", "VLO", "HFC", "DK", "DINO",
    "KMI", "WMB", "OKE", "TRGP", "ET", "EPD", "MMP", "PAA",
    # Renewables
    "NEE", "FSLR", "ENPH", "SEDG", "RUN", "NOVA",
    # Coal / commodities
    "BTU", "ARCH", "CEIX",
]

INDUSTRIALS = [
    # Defense
    "LMT", "RTX", "NOC", "GD", "HII", "TXT", "LDOS", "SAIC", "CACI", "BAE",
    # Aerospace
    "BA", "HWM", "TDG", "SPR", "HEICO", "TransDigm",
    # Industrial conglomerates
    "HON", "GE", "MMM", "EMR", "ROK", "PH", "ITW", "ETN",
    # Machinery / equipment
    "CAT", "DE", "IR", "IEX", "AME", "ROP", "GNRC", "CARR", "OTIS",
    "AIRC", "GTES", "XPO",
    # Construction / materials
    "MLM", "VMC", "EXP", "SUM", "USCR",
    # Logistics / transportation
    "UPS", "FDX", "JBHT", "ODFL", "SAIA", "XPO", "CHRW",
    "UAL", "DAL", "LUV", "AAL", "JBLU", "ALK",
    "CSX", "NSC", "UNP", "CP", "CNI",
    # Staffing / services
    "FAST", "GWW", "MSM", "AIT", "WSO", "MRC",
    # Waste / environmental
    "WM", "RSG", "CWST", "US",
    # Engineering / consulting
    "J", "PRLB", "EXPO",
]

MATERIALS = [
    # Chemicals
    "LIN", "APD", "ECL", "DD", "DOW", "LYB", "CE", "EMN", "OLN",
    "CF", "MOS", "NTR", "FMC", "ALB", "MP",
    # Coatings / specialty
    "PPG", "SHW", "RPM",
    # Metals / mining
    "FCX", "NEM", "GOLD", "AEM", "WPM", "SCCO", "AA", "NUE", "STLD",
    "RS", "CMC", "ATI", "HWM", "X", "CLF",
    # Packaging
    "CCK", "IP", "SEE", "AMCR", "AVY", "PKG", "SON",
    # Paper / forest
    "WY", "RYN", "PCH",
]

REAL_ESTATE = [
    # Towers / data
    "AMT", "CCI", "EQIX", "SBAC", "DLR", "CONE", "QTS",
    # Industrial / logistics
    "PLD", "EXR", "REXR", "FR", "EGP",
    # Residential
    "PSA", "AVB", "EQR", "INVH", "MAA", "UDR", "CPT", "NMD",
    # Office
    "BXP", "VNO", "SLG", "HIW",
    # Retail
    "SPG", "O", "NNN", "VICI", "GLPI", "KIM", "REG", "FRT", "MAC",
    # Healthcare REIT
    "VTR", "WELL", "DOC", "MPW",
    # Specialty
    "WY", "IRM", "COR", "CBRE", "JLL",
]

UTILITIES = [
    "NEE", "DUK", "SO", "D", "AEP", "EXC", "SRE", "PCG", "ES", "XEL",
    "WEC", "DTE", "ED", "CMS", "ETR", "CNP", "NI", "AEE", "LNT",
    "EVRG", "PNW", "AWK", "FE",
]

SECTOR_ETFS = [
    "SPY", "QQQ", "IWM", "DIA",
    "XLK", "XLF", "XLV", "XLE", "XLI", "XLC", "XLRE", "XLU", "XLY", "XLP", "XLB",
    "VXX", "GLD", "SLV", "GDX", "GDXJ", "TLT", "HYG", "IEF", "AGG",
    "ARKK", "ARKG", "SOXX", "SMH", "IBB",
    "EEM", "EFA", "VEA", "IEMG",
]

# Combine all sectors, deduplicate, sort
_all = (
    TECHNOLOGY + COMMUNICATION_SERVICES + CONSUMER_DISCRETIONARY +
    CONSUMER_STAPLES + HEALTHCARE + FINANCIALS + ENERGY +
    INDUSTRIALS + MATERIALS + REAL_ESTATE + UTILITIES + SECTOR_ETFS
)

FULL_UNIVERSE = sorted(list(dict.fromkeys(t for t in _all if t and len(t) >= 1)))

if __name__ == "__main__":
    print(f"Full universe: {len(FULL_UNIVERSE)} tickers")
    # Show by sector
    sectors = {
        "Technology": TECHNOLOGY,
        "Communication": COMMUNICATION_SERVICES,
        "Consumer Disc": CONSUMER_DISCRETIONARY,
        "Consumer Stap": CONSUMER_STAPLES,
        "Healthcare": HEALTHCARE,
        "Financials": FINANCIALS,
        "Energy": ENERGY,
        "Industrials": INDUSTRIALS,
        "Materials": MATERIALS,
        "Real Estate": REAL_ESTATE,
        "Utilities": UTILITIES,
        "ETFs": SECTOR_ETFS,
    }
    for name, tickers in sectors.items():
        uniq = list(dict.fromkeys(tickers))
        print(f"  {name:16}: {len(uniq):3} tickers")
