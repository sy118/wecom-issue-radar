# Tasks

## 1. 妯″瀷鍗曚竴浜嬪疄鏉ユ簮锛堝悗绔級

- [x] 1.1 鍦?`worker/wechat/service.py` 瀹炵幇 `_ensure_default_provider()` 鐨?瀛樺湪鍗冲榻?锛歚llm-default` 瀛樺湪鏃剁敤褰撳墠 `config.llm` 鏇存柊 kind/baseUrl/model/temperature/maxOutputTokens锛屼繚鐣?`secret_ref`锛涗笉瀛樺湪涓旀棤鑷畾涔?provider 鏃舵墠鍒涘缓銆傞獙璇侊細鏂板/鏇存柊 `tests/` 涓?provider 鍚屾鍗曟祴锛岃鐩?宸叉湁杩炴帴璺熼殢鍏ㄥ眬鍙樺寲""瀵嗛挜淇濈暀""鑷畾涔夎繛鎺ヤ笉鍙楀奖鍝?銆?- [x] 1.2 纭 `worker/wechat/actions.py` 鐨?`_providers` 鍝嶅簲缁х画鍖呭惈瀹炴椂 `llm` 蹇収锛坄describe_llm_config`锛夛紝骞惰ˉ涓€鏉″搷搴旂粨鏋勬柇瑷€銆傞獙璇侊細`python -m unittest discover -s tests` 閫氳繃銆?
## 2. 鍓嶇妯″瀷灞曠ず涓€鑷存€?
- [x] 2.1 鎵╁睍 `src/pages/wechat/logic.ts` 鐨?`normalizeProviders()` 涓?`src/pages/wechat/types.ts`锛屼繚鐣?`llm`锛堝崗璁?妯″瀷/BaseURL/宸查厤缃?鏈夊瘑閽ワ級锛涜ˉ vitest 瑕嗙洊鏃у搷搴旂己灏?`llm` 鐨勫吋瀹硅矾寰勩€傞獙璇侊細`pnpm test -- src/pages/wechat/logic.test.ts` 閫氳繃銆?- [x] 2.2 鏀归€?`src/pages/wechat/WechatConfigPage.tsx` 涓?`src/components/wechat/WechatSettingsDrawer.tsx` 鐨勩€屾ā鍨嬭繛鎺ャ€嶅崱鐗囷細浼樺厛灞曠ず瀹炴椂 `llm` 骞舵爣娉ㄣ€屾潵婧愶細鍏ㄥ眬璁剧疆銆嶏紝蹇収鍏滃簳骞惰鏄庝负缂撳瓨銆傞獙璇侊細缁勪欢娓叉煋娴嬭瘯鏂█鍏ㄥ眬妯″瀷鍙樺寲鍚庡崱鐗囨枃鏈洿鏂般€?- [x] 2.3 淇 `src/pages/SettingsPage.tsx` 鐨?Agent v2 鑳藉姏鍗＄墖锛氫富鍊兼亽涓哄綋鍓?`draft.llm.model`锛涗笂娆℃娴嬫ā鍨嬪彧鍦ㄣ€屼笂娆℃娴嬶紙宸茶繃鏈燂級銆嶅壇琛屽嚭鐜般€傞獙璇侊細鏂板 `SettingsPage` 娓叉煋娴嬭瘯锛屾ā鎷熸寚绾逛笉鍖归厤鏃舵棫妯″瀷涓嶅嚭鐜板湪涓诲€间綅缃€?- [ ] 2.4 璁╀釜浜哄井淇?`DraftPanel`/`ProjectEditorDrawer` 鐨勬ā鍨嬫爣绛句笌鍏ㄥ眬妯″瀷涓€鑷达紙浠嶄互 provider 閫夋嫨涓哄噯锛屼絾榛樿椤规爣绛句娇鐢ㄥ疄鏃舵ā鍨嬪悕锛夈€傞獙璇侊細鐩稿叧娓叉煋娴嬭瘯閫氳繃銆?
## 3. UI 鍩虹灞傦細token銆佸３灞傘€佸叡浜粍浠?
- [x] 3.1 鎵╁睍 `src/index.css` 椤堕儴 token锛氳〃闈?鏂囧瓧/杈规/鐘舵€佽壊銆佸瓧鍙疯楂樸€?px 闂磋窛銆佸渾瑙掋€佷袱绾ч槾褰便€佸姩鏁堟椂闀夸笌缂撳姩锛屾祬娣辫壊鎴愬锛沗prefers-reduced-motion` 闄嶇骇銆傞獙璇侊細`pnpm typecheck` 閫氳繃锛屾祬鑹?娣辫壊鎴浘鏃犵函鑹茬己澶便€?- [ ] 3.2 缁熶竴澹冲眰锛歚titlebar`銆乣AppSidebar`/`nav.css` 鍒嗙粍涓庨€変腑鎬併€侀〉澶存ā寮忋€佸崟婊氬姩瀹瑰櫒銆佸唴瀹瑰搴︿笌鍖哄潡鑺傚銆傞獙璇侊細`src/navigation.test.ts`銆乣src/lib/readability.test.ts` 閫氳繃锛沗scripts/ui-preview.mjs` 椤甸潰鎴浘鏃犳孩鍑恒€?- [ ] 3.3 鏀舵暃鍏变韩缁勪欢 `src/components/ui.tsx` 涓?`overlay.css`锛氭寜閽彉浣撱€佸瓧娈?hint/error銆佸崱鐗?SectionHeader銆佺姸鎬?chip銆佺┖鐘舵€併€佺粺璁″潡銆佹娊灞?寮圭獥 header/footer 涓庣劍鐐圭鐞嗐€傞獙璇侊細`src/components/Overlay.test.ts`銆乣src/pages/ReplyRuntimePages.test.ts` 绛夋棦鏈夋祴璇曢€氳繃銆?
## 4. UI 涓汉寰俊

- [x] 4.1 绮句慨涓夋爮浼氳瘽椤碉紙椤圭洰鍒?浼氳瘽/鑽夌锛夛細闈㈡澘鍒嗛殧銆佹秷鎭皵娉¤妭濂忋€佽崏绋挎搷浣滃尯灞傜骇銆佺┖鐘舵€併€傞獙璇侊細`src/pages/wechat/*.test.ts(x)` 涓?`WechatWorkbenchPage` 娓叉煋娴嬭瘯閫氳繃锛屾祬鑹?娣辫壊鎴浘瀵圭収銆?- [ ] 4.2 绮句慨涓汉寰俊閰嶇疆涓績锛氬垎鍖哄崱鐗囥€佸浐瀹氫繚瀛樻潯銆侀珮绾ч」鎶樺彔銆佸嵄闄╂搷浣滃垎绂汇€佹ā鍨嬫潵婧愭爣璇嗐€傞獙璇侊細`src/pages/wechat/logic.test.ts` 涓庨厤缃腑蹇冩覆鏌撴祴璇曢€氳繃锛屾埅鍥炬棤瑁佸垏銆?
## 5. UI 浼佷笟寰俊

- [x] 5.1 绮句慨宸ヤ綔鍙颁笌闂闆疯揪锛坄WorkbenchPage`銆乣RunPage`銆乣run.css`锛夛細鎬昏鍗°€佽繘搴︿笌缇ょ粨鏋滆〃銆佺┖鐘舵€併€佷富鎿嶄綔鍞竴銆傞獙璇侊細`RunPage.analysis.test.ts` 涓庢覆鏌撴祴璇曢€氳繃銆?- [ ] 5.2 绮句慨瀹氭椂浠诲姟涓庣兢鐩戝惉锛坄SchedulesPage`銆乣GroupReplyPage`銆乣groupreply/*`銆乣groupreply.css`锛夛細鍒楄〃鍗°€佸伐浣滈槦鍒椼€佹娊灞夋楠ゃ€佺姸鎬佸窘鏍囥€佺獎绐楀彛甯冨眬銆傞獙璇侊細`SchedulesPage*.test`銆乣ReplyRuntimePages.test.ts`銆乣GroupReply*.test` 閫氳繃銆?
## 6. UI 娌熼€氬姪鎵?/ 绛栫暐 / 璁剧疆 / 鍏充簬

- [x] 6.1 绮句慨娌熼€氬姪鎵嬩笁椤典笌 `daily.css`锛氳緭鍏ュ尯銆佺粨鏋滃崱銆佽瘽鏈垪琛ㄣ€佺┖鐘舵€佷笌澶嶅埗鎿嶄綔銆傞獙璇侊細`src/pages/daily/*.test.ts(x)` 閫氳繃銆?- [ ] 6.2 绮句慨绛栫暐涓庡彂甯冨洓椤碉紙鎻愮ず璇嶅簱銆佸彂甯冪洰鏍囥€丮CP銆丏ify锛夛細琛ㄥ崟灞傜骇銆乀ab銆佹槧灏勮〃銆侀瑙堝崱銆傞獙璇侊細`PromptsPage.layout.test.ts`銆乣src/pages/publish/render.test.tsx` 閫氳繃銆?- [ ] 6.3 绮句慨璁剧疆涓庡叧浜庯細甯哥敤/楂樼骇鍒囨崲銆佸嵄闄╁尯銆侀厤缃浠借鏄庛€佺増鏈俊鎭€傞獙璇侊細`src/pages/ReplyRuntimePages.test.ts` 涓?`SettingsHome` 娓叉煋娴嬭瘯閫氳繃銆?
## 7. 鏂囨。閲嶅啓

- [x] 7.1 閲嶅啓 `README.md`锛氬畾浣?鈫?鍔熻兘鍦板浘 鈫?5 鍒嗛挓涓婃墜 鈫?鏁版嵁涓庡畨鍏?鈫?鏋舵瀯 鈫?寮€鍙戜笌楠岃瘉 鈫?鍙戝竷 鈫?FAQ/鎺掗殰 鈫?鏂囨。绱㈠紩锛涚Щ闄よ繃鏃剁増鏈彿涓庨噸澶嶅彊杩般€傞獙璇侊細瀹夎/鏋勫缓鍛戒护涓庣幇鏈?`package.json` scripts 涓€鑷达紝閾炬帴鎸囧悜瀛樺湪鐨勬枃浠躲€?- [ ] 7.2 鏂板 `docs/README.md` 鏂囨。绱㈠紩锛屽苟鍚屾 `docs/release.md`銆乣docs/electron-migration.md`銆乣docs/agent-runtime-v2.md` 涓笌 v4.5.0 涓嶄竴鑷寸殑鎻忚堪銆傞獙璇侊細鎵€鏈夌浉瀵归摼鎺ュ彲杈撅紝鐗堟湰鍙蜂笌 `package.json` 涓€鑷淬€?- [ ] 7.3 鏇存柊 `CHANGELOG.md` 澧炲姞 v4.5.0锛氭ā鍨嬬湡鐩镐慨澶嶃€乁I 绮句慨銆佹枃妗ｉ噸鍐欍€佸彂甯冭鏄庛€傞獙璇侊細鏉＄洰涓庢湰娆℃彁浜よ寖鍥翠竴鑷淬€?
## 8. 闆嗘垚楠岃瘉涓庡彂甯?
- [x] 8.1 鏈湴闂ㄧ锛歚pnpm test`銆乣pnpm typecheck`銆乣pnpm build:renderer`銆乣python -m unittest discover -s tests` 鍏ㄩ儴閫氳繃锛堜笉鎵ц PyInstaller/NSIS锛夈€?- [ ] 8.2 瑙嗚楠屾敹锛歚scripts/ui-preview.mjs` 鐢熸垚娴呰壊/娣辫壊鍏抽敭椤甸潰鎴浘锛屾鏌ユ孩鍑恒€佸姣斿害銆佺劍鐐逛笌绌虹姸鎬侊紝骞惰褰曞埌鍙樻洿璇存槑銆?- [ ] 8.3 鐗堟湰鍗囩骇鍒?`4.5.0`锛堜粎 `package.json`锛夛紝纭鏃?`src-tauri` 鐗堟湰鏀瑰姩锛涙彁浜ゅ苟鎺ㄩ€?`main`锛岀敤 `gh run list` 纭 CI 涓?Windows 鏋勫缓鎴愬姛銆丄rtifact 鍙笅杞姐€?- [ ] 8.4 鎺ㄩ€?tag `v4.5.0`锛岀敤 `gh run watch` 涓?`gh release view` 楠岃瘉 NSIS 瀹夎鍖呫€乣latest.yml`銆乥lockmap 涓庡吋瀹?`latest.json` 宸插彂甯冦€?
## Workflow follow-up

- 鍙樻洿褰掓。锛氬湪瀹炵幇涓庡彂甯冮獙璇佸畬鎴愬悗锛屾寜椤圭洰瀹℃煡瑕佹眰褰掓。璇ュ彉鏇淬€?