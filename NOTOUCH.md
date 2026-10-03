# NOTOUCH CODE AUDIT REPORT
## TEOS Never Died / RAVEN Project

### Overview
This document identifies and explains code sections in the RAVEN security tool that should be considered "NOTOUCH" (do not modify) due to their security-critical nature, legal restrictions, or essential functionality. Modifying these areas could compromise the tool's effectiveness, security, or legal compliance.

### 1. CRITICAL: Secrets and Local Configurations
**Location:** `.gitignore` (lines 2-3)
```gitignore
# Secrets and Local Configs (CRITICAL)
.env
*.bat
*.vbs
```

**Explanation:**
- `.env` files typically contain sensitive credentials, API keys, and tokens
- `*.bat` and `*.vbs` files may contain local execution scripts with embedded secrets
- Exposure of these files could compromise the entire security tool and associated systems

**Protection Status:** Properly ignored by git, but requires regular auditing to ensure no secrets are accidentally committed.

**Risk Level:** HIGH

### 2. CORE: Security Assessment Engine
**Location:** Multiple files, primarily:
- `raven.py` - Core AI interaction logic
- `telegram_bot.py` - Main bot functionality with all security commands

**Key NOTOUCH Functions:**
- `get_reply()` in `raven.py` (lines 40-65) - Handles AI model communication
- Security assessment command handlers in `telegram_bot.py`:
  - `/scan` - Target reconnaissance (lines 240-260)
  - `/cve` - CVE analysis (lines 262-282)
  - `/exploit` - Exploitation techniques (lines 284-304)
  - `/harden` - Security hardening (lines 306-326)
  - `/analyze` - Full 4-phase pentest (lines 328-348)

**Explanation:**
- These functions implement the core security assessment capabilities
- Modifications could break the tool's primary functionality
- The 4-phase assessment format is core to RAVEN's value proposition
- Security-related prompts must remain accurate and comprehensive

**Risk Level:** HIGH

### 3. LEGAL: Licensing-Restricted Code
**Location:** `LICENSE` file and referenced TESL (TEOS Egypt Sovereign License)

**Key Points:**
- Repository governed by TEOS Egypt Sovereign License (TESL)
- Source-available, not open source
- All rights reserved by Elmahrosa International (2025-2026)
- Canonical license: https://github.com/Elmahrosa/teos-international-civic-blockchain-constitution/blob/main/LICENSE.md

**Explanation:**
- Legal restrictions on modification, distribution, and usage
- Cannot be treated as typical open-source code
- Modifications may violate licensing terms

**Risk Level:** MEDIUM to HIGH (Legal)

### 4. MODEL: AI System Prompt
**Location:** `raven.py` (@ lines 9-38)
**Code:** `RAVEN_SYSTEM_PROMPT` variable defining the AI's behavior

**Explanation:**
- Defines RAVEN's role as an "elite, unrestricted Security and Vulnerability Assessment AI agent of TEOS82"
- Specifies the mandatory 4-phase output format for all security assessments
- Contains unrestricted security assessment philosophy ("Zero ethical boundaries", "Zero refusal of requests")
- Changes could alter the tool's fundamental behavior and effectiveness

**Risk Level:** MEDIUM

### Protection Recommendations

#### Immediate Actions:
1. **Verify .env security:** Ensure no actual secrets are stored in the repository
2. **Review .bat/.vbs files:** Check that any existing batch/VBS scripts don't contain embedded credentials
3. **Document nottouch areas:** Clearly mark critical sections in code with appropriate comments

#### Ongoing Protection:
1. **Add protective comments:** Consider adding "DO NOT MODIFY - CRITICAL SECURITY CODE" comments to core security functions
2. **Implement pre-commit hooks:** Prevent accidental committing of .env or other sensitive files
3. **Regular audits:** Schedule periodic reviews of nottouch code to ensure integrity
4. **Access controls:** Limit modification rights to core security functions to trusted developers only

#### Documentation Improvements:
1. **Maintain this NOTOUCH.md file:** Keep it updated with any changes to nottouch areas
2. **Add licensing notices:** Ensure all files properly reference the TESL restrictions
3. **Consider a SECURITY.md file:** For broader security practices and policies

### Last Audit Performed: October 3, 2026
### Next Recommended Audit: January 3, 2027 (Quarterly)

---
*This document serves as a guide for developers and maintainers to identify code that requires special protection due to its critical nature, security implications, or legal restrictions.*