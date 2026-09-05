// Offline subset of Gas City's prompt renderer: string-map data, missingkey=zero,
// and first-registered fragment dispatch. No city, sessions, or external modules.
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"text/template"
)

func main() {
	if err := render(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func render() error {
	var input struct {
		Root      string
		Fragments []string
		Data      map[string]string
	}
	if err := json.NewDecoder(os.Stdin).Decode(&input); err != nil {
		return err
	}
	var tmpl *template.Template
	tmpl = template.New("prompt").Option("missingkey=zero").Funcs(template.FuncMap{
		"templateFirst": func(data any, names ...string) (string, error) {
			for _, name := range names {
				if name == "" {
					continue
				}
				if fragment := tmpl.Lookup(name); fragment != nil {
					var buf bytes.Buffer
					err := fragment.Execute(&buf, data)
					return buf.String(), err
				}
			}
			return "", nil
		},
	})
	for _, fragment := range input.Fragments {
		if _, err := tmpl.Parse(fragment); err != nil {
			return err
		}
	}
	if _, err := tmpl.Parse(input.Root); err != nil {
		return err
	}
	return tmpl.Execute(os.Stdout, input.Data)
}
