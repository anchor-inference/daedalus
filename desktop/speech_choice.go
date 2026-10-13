package main

// The speech models the wizard's speech step offers. The list is the app's own catalog
// (daedalus/speech/catalog.py), carried here as a copy because the wizard runs before the app is
// installed and so cannot ask it; tests/unit/test_speech_setup_model.py holds the copy to the
// catalog. The wizard only writes the choice down (DAEDALUS_STT_MODEL); the app downloads the model
// once it is up, on the downloads Settings → Voice & speech shows.

import (
	_ "embed"
	"encoding/json"
)

//go:embed speech_models.json
var speechModelsJSON []byte

// speechModel is one entry as the wizard draws it: what it hears, what it costs, and the two bars.
type speechModel struct {
	ID             string   `json:"id"`
	Label          string   `json:"label"`
	Languages      []string `json:"languages"`
	LanguageCount  int      `json:"language_count"`
	SizeBytes      int64    `json:"size_bytes"`
	MemoryMB       int      `json:"memory_mb"`
	Accuracy       int      `json:"accuracy"`
	Speed          int      `json:"speed"`
	RecommendedFor []string `json:"recommended_for"`
}

// speechModels is the copy, read once. A copy that does not parse is a build that cannot set
// speech up, which the tests catch long before a release does.
var speechModels = func() []speechModel {
	var out []speechModel
	if err := json.Unmarshal(speechModelsJSON, &out); err != nil {
		panic("speech_models.json: " + err.Error())
	}
	return out
}()

// knownSpeechModel reports whether the wizard offers this model.
func knownSpeechModel(id string) bool {
	for _, model := range speechModels {
		if model.ID == id {
			return true
		}
	}
	return false
}

// speechChoice is the step's answer as the page holds it: off, cloud, or a model's id.
func speechChoice(s Setup) string {
	switch {
	case s.Voice == "cloud":
		return "cloud"
	case s.Voice == "local" && knownSpeechModel(s.SttModel):
		return s.SttModel
	}
	return "off"
}
