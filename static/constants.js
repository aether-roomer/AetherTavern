/* Shared enum values (keep in sync with server/models.py). */

// The 9 themes shown in the Settings picker (3x3 grid).
// `toggleable` themes are the pair the toolbar sun/moon button flips between;
// any other selection hides that toggle until the user picks one of them again.
export const THEMES = [
  { id: 'light',     name: 'Light',     toggleable: true  },
  { id: 'dark',      name: 'Dark',      toggleable: true  },
  { id: 'sepia',     name: 'Sepia',     toggleable: false },
  { id: 'noir',      name: 'Noir',      toggleable: false },
  { id: 'pastel',    name: 'Pastel',    toggleable: false },
  { id: 'forest',    name: 'Forest',    toggleable: false },
  { id: 'terminal',  name: 'Terminal',  toggleable: false },
  { id: 'nebula',    name: 'Nebula',    toggleable: false },
  { id: 'solarized', name: 'Solarized', toggleable: false },
];

export const TOGGLEABLE_THEMES = new Set(
  THEMES.filter(t => t.toggleable).map(t => t.id)
);


export const EMOTIONS = [
  'angry', 'aroused', 'bored', 'confused', 'determined', 'disgusted',
  'embarrassed', 'excited', 'happy', 'hurt', 'irritated', 'laughing',
  'love', 'nervous', 'neutral', 'playful', 'sad', 'scared',
  'shy', 'smug', 'surprised', 'thinking', 'tired', 'worried',
];

export const INTIMACIES = ['stranger', 'acquaintance', 'close', 'romantic'];

export const STYLES = ['chat', 'roleplay'];

export const RESPONSE_LENGTHS = [
  '', 'short', 'medium', 'long', 'very long', 'para',
  'rapid spam', 'spam', 'spammy short', 'spammy medium',
  'spammy long', 'spammy very long', 'spammy para',
];

// Suggestions surfaced via <datalist> on the gender / pronouns text
// inputs. Free-text is still accepted; this just nudges users toward the
// canonical lowercase / Japanese-pair formats so the AER prompt builder
// gets clean values. The placeholders mirror the same idea — visible
// before the user types, hinting at format and that the field is
// open-ended.
export const GENDER_SUGGESTIONS = [
  'female', 'male', 'non-binary', 'androgynous', 'agender', 'genderfluid',
  '女性', '男性',
];
export const GENDER_PLACEHOLDER = 'female, male, non-binary, 女性, 男性, …';
// Pronouns are slash-separated in both English and Japanese. The Japanese
// pair carries the possessive marker (の) on the second half.
export const PRONOUN_SUGGESTIONS = [
  'she/her', 'he/him', 'they/them', 'she/they', 'he/they', 'it/its',
  '彼女/彼女の', '彼/彼の', '彼ら/彼らの',
];
export const PRONOUN_PLACEHOLDER = 'she/her, he/him, they/them, 彼女/彼女の, …';
export const SPECIES_PLACEHOLDER = 'human, elf, android, microwave, …';


// --- TTS -----------------------------------------------------------------

export const TTS_GLOBAL_MODES  = ['off', 'default_off', 'default_on'];
export const ENTITY_TTS_MODES  = ['default', 'enabled', 'disabled'];
export const TTS_PROVIDER_KINDS = ['novelai', 'openrouter', 'nanogpt', 'generic'];

// Sentinel for the "(custom)" voice picker option. When the stored
// `voice` equals this, the proxy receives `voice=-1` plus the seed
// string typed into the custom-seed input.
export const NAI_CUSTOM_SENTINEL = '__custom__';

// V1: preset name -> numeric voice id + gender (F/M/U).
// Sentinel "(custom)" maps to voice=-1 with a free-form seed string.
export const NAI_V1_PRESETS = [
  { name: 'Cyllene',  id: 17,  gender: 'F' },
  { name: 'Leucosia', id: 95,  gender: 'F' },
  { name: 'Crina',    id: 44,  gender: 'F' },
  { name: 'Hespe',    id: 80,  gender: 'F' },
  { name: 'Ida',      id: 106, gender: 'F' },
  { name: 'Alseid',   id: 6,   gender: 'M' },
  { name: 'Daphnis',  id: 10,  gender: 'M' },
  { name: 'Echo',     id: 16,  gender: 'M' },
  { name: 'Thel',     id: 41,  gender: 'M' },
  { name: 'Nomios',   id: 77,  gender: 'M' },
];

// V2: preset name -> seed string + gender (note Ligeia → "Anananan").
// `voice` is always -1 upstream for v2; the seed string drives the
// voice. Sentinel leaves seed user-controlled via custom_seed.
export const NAI_V2_PRESETS = [
  { name: 'Ligeia', seed: 'Anananan', gender: 'U' },
  { name: 'Aini',   seed: 'Aini',     gender: 'F' },
  { name: 'Orea',   seed: 'Orea',     gender: 'F' },
  { name: 'Claea',  seed: 'Claea',    gender: 'F' },
  { name: 'Lim',    seed: 'Lim',      gender: 'F' },
  { name: 'Aurae',  seed: 'Aurae',    gender: 'F' },
  { name: 'Naia',   seed: 'Naia',     gender: 'F' },
  { name: 'Aulon',  seed: 'Aulon',    gender: 'M' },
  { name: 'Elei',   seed: 'Elei',     gender: 'M' },
  { name: 'Ogma',   seed: 'Ogma',     gender: 'M' },
  { name: 'Raid',   seed: 'Raid',     gender: 'M' },
  { name: 'Pega',   seed: 'Pega',     gender: 'M' },
  { name: 'Lam',    seed: 'Lam',      gender: 'M' },
];
