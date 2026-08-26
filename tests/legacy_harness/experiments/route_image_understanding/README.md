# Route-image understanding suite

Conditions: `A_full_pdf`, `B_raw_route_images`, and `C_tokenized_route_images` across five papers and nine selected route units. Read `prompts/controller_prompt.md` and `docs/ROUTE_IMAGE_SELECTION.md` before running.

The source manifest includes controller annotations used to package the selected images. The Harness reads only paper IDs and asset paths from it and never sends the manifest itself to Gemini.
